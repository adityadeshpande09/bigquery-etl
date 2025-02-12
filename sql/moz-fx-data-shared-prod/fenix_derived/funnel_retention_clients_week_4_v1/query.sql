WITH clients_retention AS (
  SELECT
    submission_date,
    client_id,
    sample_id,
    days_seen_bits,
  FROM
    fenix.baseline_clients_last_seen
  WHERE
    submission_date = @submission_date
    AND normalized_channel = "release"
),
clients_first_seen AS (
  SELECT
    first_seen_date,
    client_id,
    sample_id,
    first_reported_country,
    first_reported_isp,
    adjust_ad_group,
    adjust_campaign,
    adjust_creative,
    adjust_network,
  FROM
    -- TODO: once the view is updated, this should be updated to point to the viev rather than the table directly.
    fenix_derived.firefox_android_clients_v2
  WHERE
    -- 28 days need to elapse before calculating the week 4 and day 28 retention metrics
    first_seen_date = DATE_SUB(@submission_date, INTERVAL 27 DAY)
    AND channel = "release"
),
retention_calculation AS (
  SELECT
    clients_first_seen.*,
    BIT_COUNT(clients_retention.days_seen_bits) AS days_seen_in_first_28_days,
    mozfun.bits28.retention(clients_retention.days_seen_bits, @submission_date) AS retention,
  FROM
    clients_retention
  INNER JOIN
    clients_first_seen
  USING
    (client_id, sample_id)
)
SELECT
  @submission_date AS submission_date,
  * EXCEPT (retention) REPLACE(
    -- metric date should align with first_seen_date, if that is not the case then the query will fail.
    IF(
      retention.day_27.metric_date <> first_seen_date,
      ERROR("Metric date misaligned with first_seen_date"),
      first_seen_date
    ) AS first_seen_date
  ),
  days_seen_in_first_28_days > 1 AS repeat_first_month_user,
  -- retention UDF works on 0 index basis, that's why for example week_1 is aliased as week 2 to make it a bit more user friendly.
  -- retained_week_2 added for testing.
  retention.day_27.active_in_week_1 AS retained_week_2,
  retention.day_27.active_in_week_3 AS retained_week_4,
FROM
  retention_calculation
