-- Align the KO archive table with the operational one
-- ===================================================
--
--   `tlg-business-intelligence-prd.til.ko_operational_history_table`
--
-- The archive is meant to be the operational table's twin plus archived_at
-- and archived_run. It is created with CREATE TABLE ... LIKE, so it inherits
-- whatever shape the operational table had on the day it was created — which
-- means it can carry the same leftover columns, and can be missing anything
-- added since.
--
-- This script makes it the right shape whatever state it is in: it creates it
-- if it is not there, adds every column it is missing, and drops the ones the
-- rename left behind. Every statement is safe to run again.
--
-- RUN sql/finish_ko_operational_rename.sql FIRST. That one cleans the
-- operational table; this one then has the right shape to copy. Running them
-- the other way round works too, but step 1 below would create the archive
-- with the leftovers and step 3 would drop them again.
--
-- Run the statements one at a time and check step 4.

-- ---------------------------------------------------------------------------
-- 1. Create it if it is not there
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS
  `tlg-business-intelligence-prd.til.ko_operational_history_table`
LIKE
  `tlg-business-intelligence-prd.til.ko_operational_table`
OPTIONS (
  description = 'Shipments whose KO is resolved, moved out of til.ko_operational_table. One row per shipment.'
);

-- ---------------------------------------------------------------------------
-- 2. Every column of the operational table, plus the two of its own
-- ---------------------------------------------------------------------------
-- Listed out rather than copied from the other table, because BigQuery has no
-- "add whatever is missing" — and a list is checkable. These 40 are the
-- operational table's columns after the rename; if one is ever added there,
-- add it here too.
ALTER TABLE `tlg-business-intelligence-prd.til.ko_operational_history_table`
  ADD COLUMN IF NOT EXISTS row_insertion_date TIMESTAMP,
  ADD COLUMN IF NOT EXISTS row_last_modified_date TIMESTAMP,
  ADD COLUMN IF NOT EXISTS last_run STRING,
  ADD COLUMN IF NOT EXISTS shipment_order_number STRING,
  ADD COLUMN IF NOT EXISTS oerodp STRING,
  ADD COLUMN IF NOT EXISTS ub_imko STRING,
  ADD COLUMN IF NOT EXISTS logistic_order_number STRING,
  ADD COLUMN IF NOT EXISTS oms_shipment_number STRING,
  ADD COLUMN IF NOT EXISTS order_submit_date DATE,
  ADD COLUMN IF NOT EXISTS shipment_creation_date DATE,
  ADD COLUMN IF NOT EXISTS action STRING,
  ADD COLUMN IF NOT EXISTS initial_fulfillment_status STRING,
  ADD COLUMN IF NOT EXISTS updated_fulfillment_status STRING,
  ADD COLUMN IF NOT EXISTS location_provider STRING,
  ADD COLUMN IF NOT EXISTS ko_fixed BOOL,
  ADD COLUMN IF NOT EXISTS ko_fix_date TIMESTAMP,
  ADD COLUMN IF NOT EXISTS last_tried_by_gxo_date TIMESTAMP,
  ADD COLUMN IF NOT EXISTS gxo_result_code STRING,
  ADD COLUMN IF NOT EXISTS tried_by_gxo_after_the_fix BOOL,
  ADD COLUMN IF NOT EXISTS ko_reason STRING,
  ADD COLUMN IF NOT EXISTS ko_resolved BOOL,
  ADD COLUMN IF NOT EXISTS ko_resolved_date TIMESTAMP,
  ADD COLUMN IF NOT EXISTS source STRING,
  ADD COLUMN IF NOT EXISTS brand STRING,
  ADD COLUMN IF NOT EXISTS eligible_for_package_closure BOOL,
  ADD COLUMN IF NOT EXISTS label_shplbl_populated BOOL,
  ADD COLUMN IF NOT EXISTS label_shplbl_format STRING,
  ADD COLUMN IF NOT EXISTS label_retlbl_present BOOL,
  ADD COLUMN IF NOT EXISTS label_retlbl_populated BOOL,
  ADD COLUMN IF NOT EXISTS label_retlbl_format STRING,
  ADD COLUMN IF NOT EXISTS shipping_tracking_number STRING,
  ADD COLUMN IF NOT EXISTS return_tracking_number STRING,
  ADD COLUMN IF NOT EXISTS doc_inv_populated BOOL,
  ADD COLUMN IF NOT EXISTS doc_rpi_populated BOOL,
  ADD COLUMN IF NOT EXISTS api_call_date TIMESTAMP,
  ADD COLUMN IF NOT EXISTS batch STRING,
  ADD COLUMN IF NOT EXISTS ko_source STRING,
  ADD COLUMN IF NOT EXISTS blocking_reason STRING,
  ADD COLUMN IF NOT EXISTS action_needed_from_gxo STRING,
  ADD COLUMN IF NOT EXISTS channel STRING,
  ADD COLUMN IF NOT EXISTS in_latest_gxo_input BOOL,
  ADD COLUMN IF NOT EXISTS archived_at TIMESTAMP,
  ADD COLUMN IF NOT EXISTS archived_run STRING;

-- ---------------------------------------------------------------------------
-- 3. Drop what the rename left behind
-- ---------------------------------------------------------------------------
-- The same thirteen as in the operational table. If the archive was created
-- before that cleanup it inherited them; if it was created after, this does
-- nothing.
ALTER TABLE `tlg-business-intelligence-prd.til.ko_operational_history_table`
  DROP COLUMN IF EXISTS logistic_order,
  DROP COLUMN IF EXISTS order_date,
  DROP COLUMN IF EXISTS shipment_created,
  DROP COLUMN IF EXISTS active_task,
  DROP COLUMN IF EXISTS gxo_ko_reason,
  DROP COLUMN IF EXISTS api_called_at,
  DROP COLUMN IF EXISTS shipment_created_effective,
  DROP COLUMN IF EXISTS get_documents_error,
  DROP COLUMN IF EXISTS ko_status,
  DROP COLUMN IF EXISTS api_http_status,
  DROP COLUMN IF EXISTS api_message,
  DROP COLUMN IF EXISTS api_create_error,
  DROP COLUMN IF EXISTS times_seen;

-- ---------------------------------------------------------------------------
-- 4. Check: the archive should be the operational table plus two
-- ---------------------------------------------------------------------------
WITH
operational AS (
  SELECT column_name FROM `tlg-business-intelligence-prd.til`.INFORMATION_SCHEMA.COLUMNS
  WHERE table_name = 'ko_operational_table'
),
archive AS (
  SELECT column_name FROM `tlg-business-intelligence-prd.til`.INFORMATION_SCHEMA.COLUMNS
  WHERE table_name = 'ko_operational_history_table'
)
SELECT
  (SELECT COUNT(*) FROM operational) AS operational_columns,
  (SELECT COUNT(*) FROM archive)     AS archive_columns,
  (SELECT STRING_AGG(column_name, ', ' ORDER BY column_name) FROM (
     SELECT column_name FROM operational
     EXCEPT DISTINCT SELECT column_name FROM archive))  AS missing_from_archive,
  (SELECT STRING_AGG(column_name, ', ' ORDER BY column_name) FROM (
     SELECT column_name FROM archive
     EXCEPT DISTINCT SELECT column_name FROM operational)) AS extra_in_archive;
-- Expected: 40 and 42, missing_from_archive NULL,
-- extra_in_archive 'archived_at, archived_run'.
