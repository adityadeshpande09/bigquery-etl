"""
Get BigQuery tables inventory.

To read more on the source table, please visit:
https://cloud.google.com/bigquery/docs/information-schema-tables
"""

from argparse import ArgumentParser

from google.cloud import bigquery

DEFAULT_PROJECTS = [
    "mozdata",
    "moz-fx-data-shared-prod",
    "moz-fx-data-marketing-prod",
]

parser = ArgumentParser(description=__doc__)
parser.add_argument("--project", default="moz-fx-data-shared-prod")
# projects queries were run from that access table
parser.add_argument("--source_projects", nargs="+", default=DEFAULT_PROJECTS)
parser.add_argument("--destination_dataset", default="monitoring_derived")
parser.add_argument("--destination_table", default="bigquery_tables_inventory_v1")


def create_query(source_project):
    """Create query for a source project."""
    return f"""
        SELECT
          DATE(creation_time) AS creation_date,
          table_catalog AS project_id,
          table_schema AS dataset_id,
          table_name AS table_id,
          table_type,
          FROM `{source_project}.region-us.INFORMATION_SCHEMA.TABLES`
        ORDER BY creation_date, project_id, dataset_id, table_id, table_type
    """


def main():
    """Run query for each source project."""
    args = parser.parse_args()

    destination_table = (
        f"{args.project}.{args.destination_dataset}.{args.destination_table}"
    )

    # remove old table in case of re-run
    client = bigquery.Client(args.project)
    client.delete_table(destination_table, not_found_ok=True)

    for project in args.source_projects:
        client = bigquery.Client(project)
        query = create_query(project)
        job_config = bigquery.QueryJobConfig(
            destination=destination_table, write_disposition="WRITE_APPEND"
        )
        client.query(query, job_config=job_config).result()


if __name__ == "__main__":
    main()
