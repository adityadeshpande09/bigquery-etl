-- Generated by ./bqetl generate events_daily
CREATE OR REPLACE TABLE
  firefox_accounts_derived.event_types_v1
AS
SELECT
  * EXCEPT (submission_date)
FROM
  firefox_accounts_derived.event_types_history_v1
