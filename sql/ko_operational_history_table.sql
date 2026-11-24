-- The KO monitoring history table
-- ===============================
--
--   `tlg-business-intelligence-prd.til.ko_operational_history_table`
--
-- Where a shipment goes once its KO is resolved. Every run copies the
-- resolved rows of til.ko_operational_table here and then removes them from
-- it, so the operational table holds open shipments only and the dashboard
-- stays the working list.
--
-- The workflow creates this table itself on the first run that resolves
-- anything, with CREATE TABLE ... LIKE, so running this file is optional — it
-- is here so the table can be created ahead of time, and so its shape is
-- written down. Both statements are safe to run on a table that already
-- exists.
--
-- One row per shipment: a shipment that was archived, came back in a later
-- upload and was resolved again updates its row here rather than adding a
-- second one. archived_at then tells you when it was last archived.

CREATE TABLE IF NOT EXISTS
  `tlg-business-intelligence-prd.til.ko_operational_history_table`
LIKE
  `tlg-business-intelligence-prd.til.ko_operational_table`
OPTIONS (
  description = 'Shipments whose KO is resolved, moved out of til.ko_operational_table. One row per shipment.'
);

ALTER TABLE `tlg-business-intelligence-prd.til.ko_operational_history_table`
  ADD COLUMN IF NOT EXISTS archived_at TIMESTAMP
    OPTIONS (description = 'When this shipment was moved out of til.ko_operational_table.'),
  ADD COLUMN IF NOT EXISTS archived_run STRING
    OPTIONS (description = 'The run that moved it.');
