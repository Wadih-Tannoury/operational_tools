-- Bring archived shipments back into the operational table
-- ========================================================
--
--   from `tlg-business-intelligence-prd.til.ko_operational_history_table`
--   to   `tlg-business-intelligence-prd.til.ko_operational_table`
--
-- For shipments archived by a resolve rule that has since changed. The rows
-- are copied back with ko_resolved cleared, so the workflow picks them up
-- again on the next run, and then removed from the archive.
--
-- WHICH ROWS
-- Step 0 sets the scope. As written it restores EVERYTHING in the archive.
-- Narrow it there rather than editing the statements below — every step reads
-- the same temporary table, so they cannot drift apart.
--
-- RUN THE STATEMENTS ONE AT A TIME, with the workflow idle, and check step 1
-- before running step 3.

-- ---------------------------------------------------------------------------
-- 0. The rows to restore
-- ---------------------------------------------------------------------------
CREATE OR REPLACE TABLE `tlg-business-intelligence-prd.til.ko_restore_scope` AS
SELECT *
FROM `tlg-business-intelligence-prd.til.ko_operational_history_table`
WHERE TRUE
  -- Narrow here. Some examples, to uncomment as needed:
  --
  -- only the GXO ones, which the old rule archived on a SHIPPED event or on a
  -- run that had no workbook:
  -- AND UPPER(IFNULL(location_provider, '')) = 'GXO'
  --
  -- only what this workflow archived today:
  -- AND DATE(archived_at) = CURRENT_DATE()
  --
  -- only one run:
  -- AND archived_run = '20261122-142009'
  --
  -- only shipments the monitoring table still calls blocked:
  -- AND UPPER(TRIM(shipment_order_number)) IN (
  --   SELECT UPPER(TRIM(CAST(shipment_order_number AS STRING)))
  --   FROM `tlg-business-intelligence-prd.til.prd_fulfillment_monitoring_table`
  --   WHERE blocking_reason IS NOT NULL
  --     AND TRIM(CAST(blocking_reason AS STRING)) NOT IN (
  --       'KO fixed, already shipped',
  --       'Shipment packed at GXO but not shipped and still READY in OMS',
  --       'GXO shared the wrong quantity')
  --     AND UPPER(TRIM(IFNULL(CAST(shipmentStatus AS STRING), '')))
  --           NOT IN ('BACKORDER', 'CUSTOMER_CARE')
  -- )
;

-- ---------------------------------------------------------------------------
-- 1. What step 3 would do
-- ---------------------------------------------------------------------------
SELECT
  COUNT(*)                                                   AS in_scope,
  COUNT(DISTINCT UPPER(TRIM(shipment_order_number)))         AS distinct_shipments,
  COUNTIF(UPPER(TRIM(shipment_order_number)) IN (
    SELECT UPPER(TRIM(shipment_order_number))
    FROM `tlg-business-intelligence-prd.til.ko_operational_table`))
                                                             AS already_back_in_the_table,
  MIN(archived_at)                                           AS archived_from,
  MAX(archived_at)                                           AS archived_to
FROM `tlg-business-intelligence-prd.til.ko_restore_scope`;
-- already_back_in_the_table is not a problem: those are updated rather than
-- inserted, and their ko_fix_date is left alone.

-- ---------------------------------------------------------------------------
-- 2. Which run archived them, in case the scope needs narrowing
-- ---------------------------------------------------------------------------
SELECT
  archived_run,
  DATE(archived_at)                                  AS archived_on,
  COUNT(*)                                           AS shipments,
  COUNTIF(UPPER(IFNULL(location_provider, '')) = 'GXO')  AS gxo,
  COUNTIF(UPPER(IFNULL(location_provider, '')) = 'MODA') AS moda
FROM `tlg-business-intelligence-prd.til.ko_restore_scope`
GROUP BY archived_run, archived_on
ORDER BY archived_on DESC, shipments DESC;

-- ---------------------------------------------------------------------------
-- 3. Put them back
-- ---------------------------------------------------------------------------
-- A MERGE rather than an INSERT: a shipment that has already come back under
-- its own steam is updated, not duplicated. ko_resolved is cleared either
-- way, which is what hands it back to the workflow.
--
-- The columns are listed out rather than INSERT ROW, because the archive has
-- two the operational table has not (archived_at, archived_run) and INSERT
-- ROW needs the two sides to match exactly. ko_resolved goes in as FALSE, so
-- a restored shipment is open from the moment it lands.
MERGE `tlg-business-intelligence-prd.til.ko_operational_table` AS T
USING `tlg-business-intelligence-prd.til.ko_restore_scope` AS S
ON UPPER(TRIM(T.shipment_order_number)) = UPPER(TRIM(S.shipment_order_number))
WHEN MATCHED THEN UPDATE SET
  ko_resolved = FALSE,
  ko_resolved_date = NULL,
  row_last_modified_date = CURRENT_TIMESTAMP()
WHEN NOT MATCHED THEN INSERT (
    row_insertion_date,
    row_last_modified_date,
    last_run,
    shipment_order_number,
    oerodp,
    ub_imko,
    logistic_order_number,
    oms_shipment_number,
    order_submit_date,
    shipment_creation_date,
    action,
    fulfillment_status,
    location_provider,
    ko_fixed,
    ko_fix_date,
    last_tried_by_gxo_date,
    gxo_result_code,
    tried_by_gxo_after_the_fix,
    ko_reason,
    ko_resolved,
    ko_resolved_date,
    source,
    brand,
    channel,
    ko_source,
    blocking_reason,
    action_needed_from_gxo,
    in_latest_gxo_input,
    eligible_for_package_closure,
    label_shplbl_populated,
    label_shplbl_format,
    label_retlbl_present,
    label_retlbl_populated,
    label_retlbl_format,
    shipping_tracking_number,
    return_tracking_number,
    doc_inv_populated,
    doc_rpi_populated,
    api_call_date,
    batch
  ) VALUES (
    S.row_insertion_date,
    CURRENT_TIMESTAMP(),
    S.last_run,
    S.shipment_order_number,
    S.oerodp,
    S.ub_imko,
    S.logistic_order_number,
    S.oms_shipment_number,
    S.order_submit_date,
    S.shipment_creation_date,
    S.action,
    S.fulfillment_status,
    S.location_provider,
    S.ko_fixed,
    S.ko_fix_date,
    S.last_tried_by_gxo_date,
    S.gxo_result_code,
    S.tried_by_gxo_after_the_fix,
    S.ko_reason,
    FALSE,
    CAST(NULL AS TIMESTAMP),
    S.source,
    S.brand,
    S.channel,
    S.ko_source,
    S.blocking_reason,
    S.action_needed_from_gxo,
    S.in_latest_gxo_input,
    S.eligible_for_package_closure,
    S.label_shplbl_populated,
    S.label_shplbl_format,
    S.label_retlbl_present,
    S.label_retlbl_populated,
    S.label_retlbl_format,
    S.shipping_tracking_number,
    S.return_tracking_number,
    S.doc_inv_populated,
    S.doc_rpi_populated,
    S.api_call_date,
    S.batch
  );

-- ---------------------------------------------------------------------------
-- 4. Take them out of the archive
-- ---------------------------------------------------------------------------
-- Only once step 3 has run. A shipment in both places would be archived again
-- on the next run that resolves it, which is harmless, but leaving it in both
-- makes the archive stop meaning "no longer open".
DELETE FROM `tlg-business-intelligence-prd.til.ko_operational_history_table`
WHERE UPPER(TRIM(shipment_order_number)) IN (
  SELECT UPPER(TRIM(shipment_order_number))
  FROM `tlg-business-intelligence-prd.til.ko_restore_scope`
);

-- ---------------------------------------------------------------------------
-- 5. Check, then clean up
-- ---------------------------------------------------------------------------
SELECT
  (SELECT COUNT(*) FROM `tlg-business-intelligence-prd.til.ko_restore_scope`)
    AS asked_to_restore,
  (SELECT COUNT(*) FROM `tlg-business-intelligence-prd.til.ko_operational_table`
   WHERE NOT IFNULL(ko_resolved, FALSE)
     AND UPPER(TRIM(shipment_order_number)) IN (
       SELECT UPPER(TRIM(shipment_order_number))
       FROM `tlg-business-intelligence-prd.til.ko_restore_scope`))
    AS back_and_open,
  (SELECT COUNT(*) FROM `tlg-business-intelligence-prd.til.ko_operational_history_table`
   WHERE UPPER(TRIM(shipment_order_number)) IN (
     SELECT UPPER(TRIM(shipment_order_number))
     FROM `tlg-business-intelligence-prd.til.ko_restore_scope`))
    AS still_in_the_archive;
-- Expected: asked_to_restore = back_and_open, still_in_the_archive = 0.

DROP TABLE IF EXISTS `tlg-business-intelligence-prd.til.ko_restore_scope`;
