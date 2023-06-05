-- Initialization query first observations for Firefox Android Clients.
WITH first_seen AS (
  SELECT
    client_id,
    sample_id,
    first_seen_date,
    submission_date,
    country AS first_reported_country,
    isp AS first_reported_isp,
    DATETIME(first_run_date) AS first_run_datetime,
    normalized_channel AS channel,
    device_manufacturer,
    device_model,
    normalized_os_version AS os_version,
    app_display_version AS app_version
  FROM
    `moz-fx-data-shared-prod.fenix.baseline_clients_first_seen`
  WHERE
    submission_date >= '2019-01-01'
    AND normalized_channel = 'release'
),
-- Find the most recent activation record per client_id. Data available since '2021-12-01'
activations AS (
  SELECT
    client_id,
    ARRAY_AGG(activated ORDER BY submission_date DESC)[SAFE_OFFSET(0)] > 0 AS activated,
  FROM
    `moz-fx-data-shared-prod.fenix.new_profile_activation`
  WHERE
    submission_date >= '2021-12-01'
  GROUP BY
    client_id
),
-- Find earliest data per client from the first_session ping.
first_session_ping AS (
  SELECT
    client_info.client_id AS client_id,
    MIN(sample_id) AS sample_id,
    DATETIME(MIN(submission_timestamp)) AS min_submission_datetime,
    MIN(SAFE.PARSE_DATETIME('%F', SUBSTR(client_info.first_run_date, 1, 10))) AS first_run_datetime,
    ARRAY_AGG(metrics.string.first_session_campaign IGNORE NULLS ORDER BY submission_timestamp ASC)[
      SAFE_OFFSET(0)
    ] AS adjust_campaign,
    ARRAY_AGG(metrics.string.first_session_network IGNORE NULLS ORDER BY submission_timestamp ASC)[
      SAFE_OFFSET(0)
    ] AS adjust_network,
    ARRAY_AGG(metrics.string.first_session_adgroup IGNORE NULLS ORDER BY submission_timestamp ASC)[
      SAFE_OFFSET(0)
    ] AS adjust_ad_group,
    ARRAY_AGG(metrics.string.first_session_creative IGNORE NULLS ORDER BY submission_timestamp ASC)[
      SAFE_OFFSET(0)
    ] AS adjust_creative
  FROM
    `moz-fx-data-shared-prod.fenix.first_session` AS fenix_first_session
  WHERE
    DATE(submission_timestamp) >= '2019-01-01'
    AND ping_info.seq = 0 -- Pings are sent in sequence, this guarantees that the first one is returned.
  GROUP BY
    client_id
),
-- Find earliest data per client from the metrics ping.
metrics_ping AS (
  -- Fenix Release
  SELECT
    client_info.client_id AS client_id,
    MIN(sample_id) AS sample_id,
    DATETIME(MIN(submission_timestamp)) AS min_submission_datetime,
    ARRAY_AGG(
      metrics.string.metrics_adjust_campaign IGNORE NULLS
      ORDER BY
        submission_timestamp ASC
    )[SAFE_OFFSET(0)] AS adjust_campaign,
    ARRAY_AGG(metrics.string.metrics_adjust_network IGNORE NULLS ORDER BY submission_timestamp ASC)[
      SAFE_OFFSET(0)
    ] AS adjust_network,
    ARRAY_AGG(
      metrics.string.metrics_adjust_ad_group IGNORE NULLS
      ORDER BY
        submission_timestamp ASC
    )[SAFE_OFFSET(0)] AS adjust_ad_group,
    ARRAY_AGG(
      metrics.string.metrics_adjust_creative IGNORE NULLS
      ORDER BY
        submission_timestamp ASC
    )[SAFE_OFFSET(0)] AS adjust_creative,
    ARRAY_AGG(metrics.string.metrics_install_source IGNORE NULLS ORDER BY submission_timestamp ASC)[
      SAFE_OFFSET(0)
    ] AS install_source
  FROM
    org_mozilla_firefox.metrics AS org_mozilla_firefox_metrics
  WHERE
    DATE(submission_timestamp) >= '2019-01-01'
  GROUP BY
    client_id
)
SELECT
  client_id,
  COALESCE(first_seen.sample_id, first_session.sample_id, metrics.sample_id) AS sample_id,
  first_seen.first_seen_date AS first_seen_date,
  first_seen.submission_date AS submission_date,
  DATE(first_seen.first_run_datetime) AS first_run_date,
  first_seen.first_reported_country AS first_reported_country,
  first_seen.first_reported_isp AS first_reported_isp,
  first_seen.channel AS channel,
  first_seen.device_manufacturer AS device_manufacturer,
  first_seen.device_model AS device_model,
  first_seen.os_version AS os_version,
  first_seen.app_version AS app_version,
  activated AS activated,
  COALESCE(first_session.adjust_campaign, metrics.adjust_campaign) AS adjust_campaign,
  COALESCE(first_session.adjust_ad_group, metrics.adjust_ad_group) AS adjust_ad_group,
  COALESCE(first_session.adjust_creative, metrics.adjust_creative) AS adjust_creative,
  COALESCE(first_session.adjust_network, metrics.adjust_network) AS adjust_network,
  metrics.install_source AS install_source,
  STRUCT(
    CASE
      WHEN first_session.client_id IS NULL
        THEN FALSE
      ELSE TRUE
    END AS reported_first_session_ping,
    CASE
      WHEN metrics.client_id IS NULL
        THEN FALSE
      ELSE TRUE
    END AS reported_metrics_ping,
    DATE(first_session.min_submission_datetime) AS min_first_session_ping_submission_date,
    DATE(first_session.first_run_datetime) AS min_first_session_ping_run_date,
    DATE(metrics.min_submission_datetime) AS min_metrics_ping_submission_date,
    CASE
      mozfun.norm.get_earliest_value(
        [
          (
            STRUCT(
              CAST(first_session.adjust_network AS STRING),
              first_session.min_submission_datetime
            )
          ),
          (STRUCT(CAST(metrics.adjust_network AS STRING), metrics.min_submission_datetime))
        ]
      )
      WHEN STRUCT(first_session.adjust_network, first_session.min_submission_datetime)
        THEN 'first_session'
      WHEN STRUCT(metrics.adjust_network, metrics.min_submission_datetime)
        THEN 'metrics'
      ELSE NULL
    END AS adjust_network__source_ping,
    CASE
      WHEN metrics.install_source IS NOT NULL
        THEN 'metrics'
      ELSE NULL
    END AS install_source__source_ping,
    mozfun.norm.get_earliest_value(
      [
        (
          STRUCT(
            CAST(first_session.adjust_network AS STRING),
            first_session.min_submission_datetime
          )
        ),
        (STRUCT(CAST(metrics.adjust_network AS STRING), metrics.min_submission_datetime))
      ]
    ).earliest_date AS adjust_network__source_ping_datetime,
    CASE
      WHEN metrics.install_source IS NOT NULL
        THEN metrics.min_submission_datetime
      ELSE NULL
    END AS install_source__source_ping_datetime
  ) AS metadata
FROM
  first_seen
FULL OUTER JOIN
  first_session_ping first_session
USING
  (client_id)
FULL OUTER JOIN
  metrics_ping AS metrics
USING
  (client_id)
LEFT JOIN
  activations
USING
  (client_id)
WHERE
  client_id IS NOT NULL
