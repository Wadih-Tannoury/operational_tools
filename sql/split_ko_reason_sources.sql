-- One KO-reason column per source
-- ===============================
--
-- A KO reason can come from four different places, and they do not always
-- agree. Until now they were collapsed into ko_reason by a fixed order of
-- preference, and whichever source won was the only one you could still see.
--
-- Each now has a column of its own, named for where it came from rather than
-- for what it means:
--
--   mon_ko_source               til.prd_fulfillment_monitoring_table.koSource
--   mon_blocking_reason                           "        .blockingReason
--   mon_external_error_message                    "        .externalErrorMessage
--   mon_error_message                             "        .errorMessage
--   b2c_action                  til.b2c_pending_shipments_analysis.action
--   gxo_ko_reason               monitoring.gxo_ko.reason
--   docs_reason                 what the documents API said when last called
--
-- Nothing here decides which one is right. ko_reason keeps working exactly
-- as before and the dashboard keeps reading it; these columns are what make
-- a later clustering possible, and what lets you see two sources disagree.
--
-- The two that already existed are renamed rather than duplicated, so no row
-- loses the history it has: blocking_reason and ko_source were always the
-- monitoring table's, and the mon_ prefix just says so.
--
-- Run 1 and 2, then check with 3.

-- ---------------------------------------------------------------------------
-- 1. The operational table
-- ---------------------------------------------------------------------------
ALTER TABLE `tlg-business-intelligence-prd.til.ko_operational_table`
  RENAME COLUMN blocking_reason TO mon_blocking_reason;

ALTER TABLE `tlg-business-intelligence-prd.til.ko_operational_table`
  RENAME COLUMN ko_source TO mon_ko_source;

ALTER TABLE `tlg-business-intelligence-prd.til.ko_operational_table`
  ADD COLUMN IF NOT EXISTS mon_external_error_message STRING
    OPTIONS (description = 'externalErrorMessage, from til.prd_fulfillment_monitoring_table.'),
  ADD COLUMN IF NOT EXISTS mon_error_message STRING
    OPTIONS (description = 'errorMessage, from til.prd_fulfillment_monitoring_table.'),
  ADD COLUMN IF NOT EXISTS b2c_action STRING
    OPTIONS (description = 'action, from til.b2c_pending_shipments_analysis.'),
  ADD COLUMN IF NOT EXISTS gxo_ko_reason STRING
    OPTIONS (description = 'reason, from monitoring.gxo_ko. Empty on rows the emailed backlog workbook inserted: that file carries no reason.'),
  ADD COLUMN IF NOT EXISTS docs_reason STRING
    OPTIONS (description = 'What the documents API said when it was last called for this shipment.');

-- ---------------------------------------------------------------------------
-- 2. The archive, which carries the same columns
-- ---------------------------------------------------------------------------
ALTER TABLE `tlg-business-intelligence-prd.til.ko_operational_history_table`
  RENAME COLUMN blocking_reason TO mon_blocking_reason;

ALTER TABLE `tlg-business-intelligence-prd.til.ko_operational_history_table`
  RENAME COLUMN ko_source TO mon_ko_source;

ALTER TABLE `tlg-business-intelligence-prd.til.ko_operational_history_table`
  ADD COLUMN IF NOT EXISTS mon_external_error_message STRING,
  ADD COLUMN IF NOT EXISTS mon_error_message STRING,
  ADD COLUMN IF NOT EXISTS b2c_action STRING,
  ADD COLUMN IF NOT EXISTS gxo_ko_reason STRING,
  ADD COLUMN IF NOT EXISTS docs_reason STRING;

-- ---------------------------------------------------------------------------
-- 3. Confirm — expect 0 old, 7 new, on both tables
-- ---------------------------------------------------------------------------
SELECT
  table_name,
  COUNTIF(column_name IN ('blocking_reason', 'ko_source')) AS old_columns_left,
  COUNTIF(column_name IN ('mon_ko_source', 'mon_blocking_reason',
                          'mon_external_error_message', 'mon_error_message',
                          'b2c_action', 'gxo_ko_reason', 'docs_reason')) AS new_columns
FROM `tlg-business-intelligence-prd.til`.INFORMATION_SCHEMA.COLUMNS
WHERE table_name IN ('ko_operational_table', 'ko_operational_history_table')
GROUP BY table_name
ORDER BY table_name;

-- ---------------------------------------------------------------------------
-- 4. Anything still reading the old names
-- ---------------------------------------------------------------------------
-- ko_dashboard_reconcile.sql and restore_ko_archived.sql both filter on
-- blocking_reason. They are documentation and recovery scripts rather than
-- anything the pipeline runs, but they will fail as written until the name is
-- changed in them too. The dashboards read the table directly, so their field
-- lists need the same treatment: the two renamed columns go from
-- blocking_reason to mon_blocking_reason and ko_source to mon_ko_source.
