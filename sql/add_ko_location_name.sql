-- Add location_name to the KO operational table and its archive
-- =============================================================
--
-- The warehouse's own name for the location a shipment is assigned to —
-- TMEXGX0058, TLITGX0058 — as against location_provider, which is the
-- warehouse behind it (GXO, MODA). The provider is derived from this, so a
-- row with a provider and no location_name has lost the thing the provider
-- was read from.
--
-- Two sources, both already in the workflow: the pending-monitoring CSV
-- carries it as "Location name", and Kibo's getShipments carries it as
-- fulfillmentLocationCode. The backfill fills it in for rows written before
-- the column existed.
--
-- Run this before deploying the new build_ko_monitoring.py, or straight after.
-- The merge builds its column list from the live schema, so a run against the
-- old schema simply does not write the column.

ALTER TABLE `tlg-business-intelligence-prd.til.ko_operational_table`
  ADD COLUMN IF NOT EXISTS location_name STRING
    OPTIONS (description =
      'The warehouse location the shipment is assigned to, as the warehouse '
      'names it (for example TMEXGX0058). location_provider is derived from it.');

ALTER TABLE `tlg-business-intelligence-prd.til.ko_operational_history_table`
  ADD COLUMN IF NOT EXISTS location_name STRING
    OPTIONS (description =
      'The warehouse location the shipment is assigned to, as the warehouse '
      'names it (for example TMEXGX0058). location_provider is derived from it.');

-- Check: both tables have it, and they still differ by exactly the archive's
-- own two columns. The archive step builds its column list from the
-- intersection of the two schemas, so they have to stay in step.
SELECT
  table_name,
  COUNT(*) AS columns,
  COUNTIF(column_name = 'location_name') AS has_location_name
FROM `tlg-business-intelligence-prd.til`.INFORMATION_SCHEMA.COLUMNS
WHERE table_name IN ('ko_operational_table', 'ko_operational_history_table')
GROUP BY table_name
ORDER BY table_name;
