WITH parsed AS (
  SELECT
    submission_timestamp,
    udf_js.parse_sponsored_interaction(udf_js.extract_string_from_bytes(payload)) AS si
  FROM
    `moz-fx-data-shared-prod.payload_bytes_error.contextual_services`
  WHERE
    DATE(submission_timestamp) = @submission_date
    AND error_type = 'SendRequest'
),
ping_data AS (
  SELECT DISTINCT
    context_id,
    "impression" AS interaction_type,
    metadata.geo.country AS country_code,
    metadata.geo.subdivision1 AS region_code,
    metadata.user_agent.os AS os_family,
    metadata.user_agent.version AS product_version,
  FROM
    `moz-fx-data-shared-prod.contextual_services_stable.quicksuggest_impression_v1`
  WHERE
    DATE(submission_timestamp) = @submission_date
    AND advertiser != "wikipedia"
    AND reporting_url IS NOT NULL
  UNION ALL
  SELECT DISTINCT
    context_id,
    "click" AS interaction_type,
    metadata.geo.country AS country_code,
    metadata.geo.subdivision1 AS region_code,
    metadata.user_agent.os AS os_family,
    metadata.user_agent.version AS product_version,
  FROM
    `moz-fx-data-shared-prod.contextual_services_stable.quicksuggest_click_v1`
  WHERE
    DATE(submission_timestamp) = @submission_date
    AND advertiser != "wikipedia"
    AND reporting_url IS NOT NULL
),
quicksuggest AS (
  SELECT
    si.contextId AS context_id,
    si.interactionType AS interaction_type,
    si.formFactor AS form_factor,
    si.flaggedFraud AS flagged_fraud,
    DATE(parsed.submission_timestamp) AS submission_date,
    IF(si.interactionType = 'impression', si.interactionCount, 0) AS impression_count,
    IF(si.interactionType = 'click', 1, 0) AS click_count
  FROM
    parsed
  WHERE
    si.source = 'quicksuggest'
)
SELECT
  form_factor,
  flagged_fraud,
  submission_date,
  country_code,
  region_code,
  os_family,
  CAST(product_version AS INT64) AS product_version,
  impression_count,
  click_count
FROM
  quicksuggest qs
LEFT JOIN
  ping_data pings
ON
  qs.context_id = pings.context_id
  AND qs.interaction_type = pings.interaction_type
