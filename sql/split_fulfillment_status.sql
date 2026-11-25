-- Split the fulfilment status into first-seen and live
-- ====================================================
--
-- One column became two:
--
--   initial_fulfillment_status   what the shipment was doing when it first
--                                became a KO. Written once, by the run that
--                                logged it, and never rewritten.
--   updated_fulfillment_status   what Kibo says it is doing now. Refreshed on
--                                every run, for every shipment the table
--                                holds open, upload or no upload.
--
-- Together they are what makes a KO explainable after the fact: the first
-- says why it was logged, the second whether that is still true.
--
-- The existing fulfillment_status column held a value that was rewritten on
-- every run, so what it holds right now is the latest reading, not the first.
-- It is renamed to initial_fulfillment_status anyway: it is the earliest
-- value anyone still has, and leaving it behind would mean starting every
-- existing row with a blank. The new column is seeded from it so neither
-- dashboard opens empty on the day of the migration. From the first run after
-- this script, the two go their separate ways.
--
-- Run 1 and 2, check 3, then 4 to drop the views that are no longer used.

-- ---------------------------------------------------------------------------
-- 1. The operational table
-- ---------------------------------------------------------------------------
ALTER TABLE `tlg-business-intelligence-prd.til.ko_operational_table`
  RENAME COLUMN fulfillment_status TO initial_fulfillment_status;

ALTER TABLE `tlg-business-intelligence-prd.til.ko_operational_table`
  ADD COLUMN IF NOT EXISTS updated_fulfillment_status STRING
    OPTIONS (description =
      'What Kibo says the shipment is doing now, refreshed every run. '
      'initial_fulfillment_status is what it was doing when it was logged.');

ALTER TABLE `tlg-business-intelligence-prd.til.ko_operational_table`
  ALTER COLUMN initial_fulfillment_status SET OPTIONS (description =
    'What the shipment was doing when it first became a KO. Written once and '
    'never rewritten; updated_fulfillment_status carries the live value.');

UPDATE `tlg-business-intelligence-prd.til.ko_operational_table`
SET updated_fulfillment_status = initial_fulfillment_status
WHERE updated_fulfillment_status IS NULL;

-- ---------------------------------------------------------------------------
-- 2. The archive, which carries the same columns
-- ---------------------------------------------------------------------------
ALTER TABLE `tlg-business-intelligence-prd.til.ko_operational_history_table`
  RENAME COLUMN fulfillment_status TO initial_fulfillment_status;

ALTER TABLE `tlg-business-intelligence-prd.til.ko_operational_history_table`
  ADD COLUMN IF NOT EXISTS updated_fulfillment_status STRING
    OPTIONS (description =
      'What Kibo said the shipment was doing when it was last refreshed.');

UPDATE `tlg-business-intelligence-prd.til.ko_operational_history_table`
SET updated_fulfillment_status = initial_fulfillment_status
WHERE updated_fulfillment_status IS NULL;

-- ---------------------------------------------------------------------------
-- 3. Confirm
-- ---------------------------------------------------------------------------
SELECT
  table_name,
  COUNTIF(column_name = 'fulfillment_status')         AS old_column_left,   -- expect 0
  COUNTIF(column_name = 'initial_fulfillment_status') AS has_initial,       -- expect 1
  COUNTIF(column_name = 'updated_fulfillment_status') AS has_updated        -- expect 1
FROM `tlg-business-intelligence-prd.til`.INFORMATION_SCHEMA.COLUMNS
WHERE table_name IN ('ko_operational_table', 'ko_operational_history_table')
GROUP BY table_name
ORDER BY table_name;

-- ---------------------------------------------------------------------------
-- 4. No dashboard views
-- ---------------------------------------------------------------------------
-- Both dashboards read ko_operational_table directly. Which of the two status
-- columns each one shows is a field choice in the dashboard, not something
-- this schema decides: the GXO board shows updated_fulfillment_status only,
-- the troubleshooting board shows both.
--
-- An earlier version of this script created ko_dashboard_gxo and
-- ko_dashboard_troubleshooting. They are not used; these drops remove them.
-- Both are safe to run when the views were never created.
DROP VIEW IF EXISTS `tlg-business-intelligence-prd.til.ko_dashboard_gxo`;
DROP VIEW IF EXISTS `tlg-business-intelligence-prd.til.ko_dashboard_troubleshooting`;

-- Confirm: expect no rows.
SELECT table_name, table_type
FROM `tlg-business-intelligence-prd.til`.INFORMATION_SCHEMA.TABLES
WHERE table_name IN ('ko_dashboard_gxo', 'ko_dashboard_troubleshooting');
