#!/usr/bin/env python3
"""clients_daily_histogram_aggregates query generator."""
import argparse
import gzip
import json
import sys
import textwrap
import urllib.request
from pathlib import Path
from time import sleep

from google.cloud import bigquery

sys.path.append(str(Path(__file__).parent.parent.parent.resolve()))
from bigquery_etl.format_sql.formatter import reformat

PROBE_INFO_SERVICE = (
    "https://probeinfo.telemetry.mozilla.org/firefox/all/main/all_probes"
)

p = argparse.ArgumentParser()
p.add_argument(
    "--agg-type", type=str, help="One of histograms/keyed_histograms", required=True,
)
p.add_argument(
    "--json-output",
    action='store_true',
    help="Output the result wrapped in json parseable as an XCOM",
)
p.add_argument(
    "--wait-seconds",
    type=int,
    default=0,
    help="Add a delay before executing the script to allow time for the xcom sidecar to complete startup",
)
p.add_argument(
    "--processes",
    type=str,
    nargs="*",
    help="Processes to include in the output.  Defaults to all processes.",
)


def generate_sql(opts, additional_queries, windowed_clause, select_clause, json_output):
    """Create a SQL query for the clients_daily_histogram_aggregates dataset."""
    query = textwrap.dedent(
        f"""-- Query generated by: sql/telemetry_derived/clients_daily_histogram_aggregates_v1.sql.py --agg-type {opts["agg_type"]}
        CREATE TEMP FUNCTION udf_aggregate_json_sum(histograms ARRAY<STRING>) AS (
          ARRAY(
            SELECT AS STRUCT
              FORMAT('%d', values_entry.key) AS key,
              SUM(values_entry.value) AS value
            FROM
              UNNEST(histograms) AS histogram,
              UNNEST(mozfun.hist.extract(histogram).values) AS values_entry
            WHERE
              histogram IS NOT NULL
            GROUP BY
              values_entry.key
            ORDER BY
              values_entry.key
          )
        );

        WITH valid_build_ids AS (
            SELECT
              DISTINCT(build.build.id) AS build_id
            FROM
              `moz-fx-data-shared-prod.telemetry.buildhub2`
        ),
        filtered AS (
            SELECT
                *,
                SPLIT(application.version, '.')[OFFSET(0)] AS app_version,
                DATE(submission_timestamp) as submission_date,
                normalized_os as os,
                application.build_id AS app_build_id,
                normalized_channel AS channel
            FROM `moz-fx-data-shared-prod.telemetry_stable.main_v4`
            INNER JOIN valid_build_ids
            ON (application.build_id = build_id)
            WHERE DATE(submission_timestamp) = @submission_date
                AND normalized_channel in (
                  "release", "beta", "nightly"
                )
                AND client_id IS NOT NULL
        ),
        sampled_data AS (
          SELECT
            *
          FROM
            filtered
          WHERE
            channel IN ("nightly", "beta")
            OR (channel = "release" AND os != "Windows")
            OR (
                channel = "release" AND
                os = "Windows" AND
                MOD(sample_id, @sample_size) = 0)
        ),

        {additional_queries}

        aggregated AS (
            {windowed_clause}
        )
        {select_clause}
        """
    )

    if json_output:
        return json.dumps(query)
    else:
        return query


def _get_keyed_histogram_sql(probes_and_buckets):
    probes = probes_and_buckets["probes"]
    buckets = probes_and_buckets["buckets"]

    probes_struct = []
    for probe, details in probes.items():
        for process in details["processes"]:
            probe_location = (
                f"payload.keyed_histograms.{probe}"
                if process == "parent"
                else f"payload.processes.{process}.keyed_histograms.{probe}"
            )
            buckets_for_probe = "{min}, {max}, {num}".format(
                min=buckets[probe]["min"],
                max=buckets[probe]["max"],
                num=buckets[probe]["n_buckets"],
            )

            agg_string = (
                f"('{probe}', "
                f"'histogram-{details['type']}', "
                f"'{process}', "
                f"{probe_location}, "
                f"({buckets_for_probe}))"
            )

            probes_struct.append(agg_string)

    probes_struct.sort()
    probes_arr = ",\n\t\t\t".join(probes_struct)

    probes_string = """
        metric,
        metric_type,
        key,
        ARRAY_AGG(bucket_range) as bucket_range,
        ARRAY_AGG(value) as value
    """

    additional_queries = f"""
        grouped_metrics AS
          (SELECT
            DATE(submission_timestamp) as submission_date,
            sample_id,
            client_id,
            normalized_os as os,
            SPLIT(application.version, '.')[OFFSET(0)] AS app_version,
            application.build_id AS app_build_id,
            normalized_channel AS channel,
            ARRAY<STRUCT<
                name STRING,
                metric_type STRING,
                process STRING,
                value ARRAY<STRUCT<key STRING, value STRING>>,
                bucket_range STRUCT<first_bucket INT64, last_bucket INT64, num_buckets INT64>
            >>[
              {probes_arr}
            ] as metrics
          FROM sampled_data),

          flattened_metrics AS
            (SELECT
              submission_date,
              sample_id,
              client_id,
              os,
              app_version,
              app_build_id,
              channel,
              process,
              metrics.name AS metric,
              metrics.metric_type AS metric_type,
              bucket_range,
              value.key AS key,
              value.value AS value
            FROM grouped_metrics
            CROSS JOIN UNNEST(metrics) AS metrics
            CROSS JOIN unnest(metrics.value) AS value),
    """

    windowed_clause = f"""
        SELECT
            submission_date,
            sample_id,
            client_id,
            os,
            app_version,
            app_build_id,
            channel,
            process,
            {probes_string}
            FROM flattened_metrics
            GROUP BY
                sample_id,
                client_id,
                submission_date,
                os,
                app_version,
                app_build_id,
                channel,
                process,
                metric,
                metric_type,
                key
    """

    select_clause = """
        SELECT
            submission_date,
            sample_id,
            client_id,
            os,
            app_version,
            app_build_id,
            channel,
            ARRAY_AGG(STRUCT<
                metric STRING,
                metric_type STRING,
                key STRING,
                process STRING,
                agg_type STRING,
                bucket_range STRUCT<
                    first_bucket INT64,
                    last_bucket INT64,
                    num_buckets INT64
                >,
                value ARRAY<STRUCT<key STRING, value INT64>>
            >(
                metric,
                metric_type,
                key,
                process,
                'summed_histogram',
                bucket_range[OFFSET(0)],
                udf_aggregate_json_sum(value)
            )) AS histogram_aggregates
        FROM aggregated
        GROUP BY
            sample_id,
            client_id,
            submission_date,
            os,
            app_version,
            app_build_id,
            channel
    """

    return {
        "additional_queries": additional_queries,
        "select_clause": select_clause,
        "windowed_clause": windowed_clause,
    }


def get_histogram_probes_sql_strings(probes_and_buckets, histogram_type):
    """Put together the subsets of SQL required to query histograms."""
    probes = probes_and_buckets["probes"]
    buckets = probes_and_buckets["buckets"]

    sql_strings = {}
    if histogram_type == "keyed_histograms":
        return _get_keyed_histogram_sql(probes_and_buckets)

    probe_structs = []
    for probe, details in probes.items():
        for process in details["processes"]:
            probe_location = (
                f"payload.histograms.{probe}"
                if process == "parent"
                else f"payload.processes.{process}.histograms.{probe}"
            )
            buckets_for_probe = "{min}, {max}, {num}".format(
                min=buckets[probe]["min"],
                max=buckets[probe]["max"],
                num=buckets[probe]["n_buckets"],
            )

            agg_string = (
                f"('{probe}', "
                f"'histogram-{details['type']}', "
                f"'{process}', "
                f"{probe_location}, "
                f"({buckets_for_probe}))"
            )

            probe_structs.append(agg_string)

    probe_structs.sort()
    probes_arr = ",\n\t\t\t".join(probe_structs)
    probes_string = f"""
            ARRAY<STRUCT<
                metric STRING,
                metric_type STRING,
                process STRING,
                value STRING,
                bucket_range STRUCT<first_bucket INT64, last_bucket INT64, num_buckets INT64>
            >> [
            {probes_arr}
        ] AS histogram_aggregates
    """

    sql_strings[
        "select_clause"
    ] = f"""
        SELECT
          sample_id,
          client_id,
          submission_date,
          os,
          app_version,
          app_build_id,
          channel,
          ARRAY_AGG(STRUCT<
            metric STRING,
            metric_type STRING,
            key STRING,
            process STRING,
            agg_type STRING,
            bucket_range STRUCT<first_bucket INT64, last_bucket INT64, num_buckets INT64>,
            value ARRAY<STRUCT<key STRING, value INT64>>
          > (metric,
            metric_type,
            '',
            process,
            'summed_histogram',
            bucket_range[OFFSET(0)],
            udf_aggregate_json_sum(value))) AS histogram_aggregates
        FROM aggregated
        GROUP BY
          1, 2, 3, 4, 5, 6, 7

    """

    sql_strings[
        "additional_queries"
    ] = f"""
        histograms AS (
            SELECT
                submission_date,
                sample_id,
                client_id,
                os,
                app_version,
                app_build_id,
                channel,
                {probes_string}
            FROM sampled_data),

        filtered_aggregates AS (
          SELECT
            submission_date,
            sample_id,
            client_id,
            os,
            app_version,
            app_build_id,
            channel,
            metric,
            metric_type,
            process,
            bucket_range,
            value
          FROM histograms
          CROSS JOIN
            UNNEST(histogram_aggregates)
          WHERE value IS NOT NULL
        ),
    """

    sql_strings[
        "windowed_clause"
    ] = f"""
      SELECT
        sample_id,
        client_id,
        submission_date,
        os,
        app_version,
        app_build_id,
        channel,
        metric,
        metric_type,
        process,
        ARRAY_AGG(bucket_range) AS bucket_range,
        ARRAY_AGG(value) AS value
      FROM filtered_aggregates
      GROUP BY
        1, 2, 3, 4, 5, 6, 7, 8, 9, 10
    """

    return sql_strings


def get_histogram_probes_and_buckets(histogram_type, processes_to_output):
    """Return relevant histogram probes."""
    project = "moz-fx-data-shared-prod"
    main_summary_histograms = {}

    client = bigquery.Client(project)
    table = client.get_table("telemetry_stable.main_v4")
    main_summary_schema = [field.to_api_repr() for field in table.schema]

    # Fetch the histograms field
    histograms_field = []
    for field in main_summary_schema:
        if field["name"] != "payload":
            continue

        for payload_field in field["fields"]:
            if payload_field["name"] == histogram_type:
                histograms_field.append(
                    {"histograms": payload_field, "process": "parent"}
                )
                continue

            if payload_field["name"] == "processes":
                for processes_field in payload_field["fields"]:
                    if processes_field["name"] in ["content", "gpu"]:
                        process_field = processes_field["name"]
                        for type_field in processes_field["fields"]:
                            if type_field["name"] == histogram_type:
                                histograms_field.append(
                                    {"histograms": type_field, "process": process_field}
                                )
                                break

    if len(histograms_field) == 0:
        return

    for histograms_and_process in histograms_field:
        for histogram in histograms_and_process["histograms"].get("fields", {}):
            if "name" not in histogram:
                continue

            processes = main_summary_histograms.setdefault(histogram["name"], set())
            if processes_to_output is None or histograms_and_process["process"] in processes_to_output:
                processes.add(histograms_and_process["process"])
            main_summary_histograms[histogram["name"]] = processes

    with urllib.request.urlopen(PROBE_INFO_SERVICE) as url:
        data = json.loads(gzip.decompress(url.read()).decode())
        histogram_probes = {
            x.replace("histogram/", "").replace(".", "_").lower()
            for x in data.keys()
            if x.startswith("histogram/")
        }

        bucket_details = {}
        relevant_probes = {
            histogram: {"processes": process}
            for histogram, process in main_summary_histograms.items()
            if histogram in histogram_probes
        }
        for key in data.keys():
            if not key.startswith("histogram/"):
                continue

            channel = "nightly"
            if "nightly" not in data[key]["history"]:
                channel = "beta"

                if "beta" not in data[key]["history"]:
                    channel = "release"

            data_details = data[key]["history"][channel][0]["details"]
            probe = key.replace("histogram/", "").replace(".", "_").lower()

            # Some keyed GPU metrics aren't correctly flagged as type
            # "keyed_histograms", so we filter those out here.
            if processes_to_output is None or "gpu" in processes_to_output:
                if data_details["keyed"] == (histogram_type == "histograms"):
                    try:
                        del relevant_probes[probe]
                    except KeyError:
                        pass
                    continue

            if probe in relevant_probes:
                relevant_probes[probe]["type"] = data_details["kind"]

            # NOTE: some probes, (e.g. POPUP_NOTIFICATION_MAINACTION_TRIGGERED_MS) have values
            # in the probe info service like 80 * 25 for the value of n_buckets.
            # So they do need to be evaluated as expressions.
            bucket_details[probe] = {
                "n_buckets": int(eval(str(data_details["n_buckets"]))),
                "min": int(eval(str(data_details["low"]))),
                "max": int(eval(str(data_details["high"]))),
            }

        return {"probes": relevant_probes, "buckets": bucket_details}


def main(argv, out=print):
    """Print a clients_daily_histogram_aggregates query to stdout."""
    opts = vars(p.parse_args(argv[1:]))
    sql_string = ""

    if opts["agg_type"] in ("histograms", "keyed_histograms"):
        probes_and_buckets = get_histogram_probes_and_buckets(opts["agg_type"], opts["processes"])
        sql_string = get_histogram_probes_sql_strings(
            probes_and_buckets, opts["agg_type"]
        )
    else:
        raise ValueError("agg-type must be one of histograms, keyed_histograms")

    sleep(opts['wait_seconds'])
    out(
        reformat(
            generate_sql(
                opts,
                sql_string.get("additional_queries", ""),
                sql_string["windowed_clause"],
                sql_string["select_clause"],
                opts["json_output"],
            )
        )
    )


if __name__ == "__main__":
    main(sys.argv)
