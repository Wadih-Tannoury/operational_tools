-- Finish the KO operational table's rename
-- ========================================
--
--   `tlg-business-intelligence-prd.til.ko_operational_table`
--
-- WHY THIS EXISTS
-- The rename was supposed to run as sql/migrate_ko_operational_table.sql,
-- which renames each column in place. What ran instead was
-- sql/ko_operational_table.sql, whose ALTER TABLE ... ADD COLUMN IF NOT EXISTS
-- added the new names beside the old ones. The table therefore carries both
-- generations: the data is under the old names and the workflow writes under
-- the new ones.
--
-- This script carries the data across and then drops the old columns. It is
-- safe to run on a table that has already been cleaned — the UPDATE matches
-- nothing and DROP COLUMN IF EXISTS does nothing — but it is NOT safe to run
-- twice while a run is in flight, so run it with the workflow idle.
--
-- RUN THE STATEMENTS ONE AT A TIME and check step 1 against step 4.

-- ---------------------------------------------------------------------------
-- 1. Before: how much data is under the old names
-- ---------------------------------------------------------------------------
SELECT
  COUNT(*)                                             AS rows_total,
  COUNTIF(logistic_order IS NOT NULL)                  AS has_logistic_order,
  COUNTIF(logistic_order_number IS NOT NULL)           AS has_logistic_order_number,
  COUNTIF(order_date IS NOT NULL)                      AS has_order_date,
  COUNTIF(order_submit_date IS NOT NULL)               AS has_order_submit_date,
  COUNTIF(shipment_created IS NOT NULL)                AS has_shipment_created,
  COUNTIF(shipment_creation_date IS NOT NULL)          AS has_shipment_creation_date,
  COUNTIF(active_task IS NOT NULL)                     AS has_active_task,
  COUNTIF(fulfillment_status IS NOT NULL)              AS has_fulfillment_status,
  COUNTIF(gxo_ko_reason IS NOT NULL)                   AS has_gxo_ko_reason,
  COUNTIF(ko_reason IS NOT NULL)                       AS has_ko_reason,
  COUNTIF(api_called_at IS NOT NULL)                   AS has_api_called_at,
  COUNTIF(api_call_date IS NOT NULL)                   AS has_api_call_date
FROM `tlg-business-intelligence-prd.til.ko_operational_table`;

-- ---------------------------------------------------------------------------
-- 2. Carry the data across
-- ---------------------------------------------------------------------------
-- COALESCE and not a plain assignment: a row the workflow has already written
-- under the new name keeps what it has. The old value only fills a gap.
--
-- The casts are there because three of the new columns are not strings:
-- order_submit_date and shipment_creation_date are DATE, api_call_date is
-- TIMESTAMP. SAFE_CAST rather than CAST, so one unparseable old value costs
-- that row its date instead of failing the whole statement.
UPDATE `tlg-business-intelligence-prd.til.ko_operational_table`
SET
  logistic_order_number  = COALESCE(logistic_order_number,
                                    CAST(logistic_order AS STRING)),
  order_submit_date      = COALESCE(order_submit_date,
                                    SAFE_CAST(CAST(order_date AS STRING) AS DATE)),
  shipment_creation_date = COALESCE(shipment_creation_date,
                                    SAFE_CAST(CAST(shipment_created AS STRING) AS DATE)),
  fulfillment_status     = COALESCE(fulfillment_status, CAST(active_task AS STRING)),
  ko_reason              = COALESCE(ko_reason, CAST(gxo_ko_reason AS STRING)),
  api_call_date          = COALESCE(api_call_date,
                                    SAFE_CAST(CAST(api_called_at AS STRING) AS TIMESTAMP))
WHERE TRUE;

-- ---------------------------------------------------------------------------
-- 2b. Translate `source` from the file formats to the systems behind them
-- ---------------------------------------------------------------------------
-- The old worker wrote which FILE a shipment came from; the new one writes
-- which SYSTEM. Every rule that reads this column looks for GXO in it — the
-- resolve pass, the console's working list, the GXO action — so a row still
-- carrying 'excel only' is invisible to all three.
--
-- The worker translates these on the fly as well, so a backfill run fixes the
-- same rows. This is here so the table is right without waiting for one.
UPDATE `tlg-business-intelligence-prd.til.ko_operational_table`
SET source = CASE LOWER(TRIM(source))
               WHEN 'csv + excel' THEN 'TLG + GXO'
               WHEN 'csv only'    THEN 'TLG only'
               WHEN 'excel only'  THEN 'GXO only'
               WHEN 'excel'       THEN 'GXO only'
               WHEN 'csv'         THEN 'TLG only'
               ELSE source
             END
WHERE LOWER(TRIM(IFNULL(source, ''))) IN
      ('csv + excel', 'csv only', 'excel only', 'excel', 'csv');

-- ---------------------------------------------------------------------------
-- 2c. location_provider for the GXO rows that never got one
-- ---------------------------------------------------------------------------
-- The column arrived after these rows were written. A shipment that came from
-- the GXO side of an upload is GXO's, so it can be filled from `source`
-- alone; the rest are left to the workflow, which reads them out of
-- til.prd_fulfillment_monitoring_table.
UPDATE `tlg-business-intelligence-prd.til.ko_operational_table`
SET location_provider = 'GXO'
WHERE NULLIF(TRIM(IFNULL(location_provider, '')), '') IS NULL
  AND STRPOS(UPPER(IFNULL(source, '')), 'GXO') > 0;

-- ---------------------------------------------------------------------------
-- 2d. action_needed_from_gxo, from what the table now says
-- ---------------------------------------------------------------------------
-- The same rule the workflow applies: the cancellation first, then the
-- printing. GXO shipments only — an action against a MODA shipment would be
-- read as something to chase GXO about.
UPDATE `tlg-business-intelligence-prd.til.ko_operational_table`
SET action_needed_from_gxo =
      CASE
        WHEN TRIM(IFNULL(blocking_reason, '')) =
             'Shipment cancelled on Kibo but still pending for GXO'
          THEN 'To cancel the shipment'
        WHEN IFNULL(ko_fixed, FALSE)
          THEN 'To print shipping documents'
        ELSE NULL
      END
WHERE UPPER(IFNULL(location_provider, '')) = 'GXO'
   OR (NULLIF(TRIM(IFNULL(location_provider, '')), '') IS NULL
       AND STRPOS(UPPER(IFNULL(source, '')), 'GXO') > 0);

-- ---------------------------------------------------------------------------
-- 3. Drop the old columns
-- ---------------------------------------------------------------------------
-- Six that were renamed, and seven that were asked to be removed outright
-- (times_seen among them: nothing has written it since the rename).
-- Dropping a column is not undoable past the table's time-travel window, so
-- do not run this until step 2 has run and step 1's counts look right.
ALTER TABLE `tlg-business-intelligence-prd.til.ko_operational_table`
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
-- 4. After: the table should hold 40 columns and no old name
-- ---------------------------------------------------------------------------
SELECT
  source,
  COUNT(*)                                            AS rows_,
  COUNTIF(IFNULL(ko_fixed, FALSE))                    AS fixed,
  COUNTIF(NULLIF(TRIM(IFNULL(location_provider, '')), '') IS NOT NULL) AS has_provider,
  COUNTIF(NULLIF(TRIM(IFNULL(action_needed_from_gxo, '')), '') IS NOT NULL) AS has_action
FROM `tlg-business-intelligence-prd.til.ko_operational_table`
GROUP BY source
ORDER BY rows_ DESC;
-- Expect only 'TLG + GXO', 'TLG only', 'GXO only' and 'MONITORING' here, and
-- every fixed GXO row carrying an action.

SELECT COUNT(*) AS columns_now
FROM `tlg-business-intelligence-prd.til`.INFORMATION_SCHEMA.COLUMNS
WHERE table_name = 'ko_operational_table';

SELECT column_name
FROM `tlg-business-intelligence-prd.til`.INFORMATION_SCHEMA.COLUMNS
WHERE table_name = 'ko_operational_table'
  AND column_name IN (
    'logistic_order', 'order_date', 'shipment_created', 'active_task',
    'gxo_ko_reason', 'api_called_at', 'shipment_created_effective',
    'get_documents_error', 'ko_status', 'api_http_status', 'api_message',
    'api_create_error', 'times_seen'
  );
-- Both of the above: 40, and no rows.
