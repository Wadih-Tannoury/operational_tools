-- Drop the per-label and per-document flag columns
-- ===============================================
--
-- These seven recorded which parts of the documents response came back
-- populated and in what format:
--
--   label_shplbl_populated   label_shplbl_format
--   label_retlbl_present     label_retlbl_populated   label_retlbl_format
--   doc_inv_populated        doc_rpi_populated
--
-- The workflow still reads every one of them on every call — they are what
-- decides ko_fixed — but it no longer stores them. The verdict and the
-- evidence that carried it stay in ko_fixed and ko_reason, which say the same
-- thing in one line ("fixed: shipping label PDF, return label PDF, both
-- tracking numbers") instead of seven columns of yes/no.
--
-- What remains of the response: eligible_for_package_closure,
-- shipping_tracking_number, return_tracking_number and api_call_date.
--
-- Run this AFTER deploying the new build_ko_monitoring.py and
-- ko_operational_table.py. Dropping first is also safe — the merge builds its
-- column list from the live schema — but a run in between would write seven
-- columns that are about to vanish.
--
-- DROP COLUMN is not free: BigQuery rewrites the table, and the data is gone
-- past the time-travel window (7 days by default). If those flags are worth
-- keeping for an audit, take a copy first:
--
--   CREATE TABLE `tlg-business-intelligence-prd.til.ko_label_flags_20261124` AS
--   SELECT shipment_order_number, api_call_date,
--          label_shplbl_populated, label_shplbl_format,
--          label_retlbl_present, label_retlbl_populated, label_retlbl_format,
--          doc_inv_populated, doc_rpi_populated
--   FROM `tlg-business-intelligence-prd.til.ko_operational_table`;

ALTER TABLE `tlg-business-intelligence-prd.til.ko_operational_table`
  DROP COLUMN IF EXISTS label_shplbl_populated,
  DROP COLUMN IF EXISTS label_shplbl_format,
  DROP COLUMN IF EXISTS label_retlbl_present,
  DROP COLUMN IF EXISTS label_retlbl_populated,
  DROP COLUMN IF EXISTS label_retlbl_format,
  DROP COLUMN IF EXISTS doc_inv_populated,
  DROP COLUMN IF EXISTS doc_rpi_populated;

ALTER TABLE `tlg-business-intelligence-prd.til.ko_operational_history_table`
  DROP COLUMN IF EXISTS label_shplbl_populated,
  DROP COLUMN IF EXISTS label_shplbl_format,
  DROP COLUMN IF EXISTS label_retlbl_present,
  DROP COLUMN IF EXISTS label_retlbl_populated,
  DROP COLUMN IF EXISTS label_retlbl_format,
  DROP COLUMN IF EXISTS doc_inv_populated,
  DROP COLUMN IF EXISTS doc_rpi_populated;

-- Check: both tables should come back with the same column count, and none of
-- the seven. The archive step builds its column list from the intersection of
-- the two schemas, so they must stay in step.
SELECT
  table_name,
  COUNT(*) AS columns,
  COUNTIF(column_name IN (
    'label_shplbl_populated', 'label_shplbl_format', 'label_retlbl_present',
    'label_retlbl_populated', 'label_retlbl_format', 'doc_inv_populated',
    'doc_rpi_populated')) AS still_there
FROM `tlg-business-intelligence-prd.til`.INFORMATION_SCHEMA.COLUMNS
WHERE table_name IN ('ko_operational_table', 'ko_operational_history_table')
GROUP BY table_name
ORDER BY table_name;
