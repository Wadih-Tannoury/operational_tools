-- Why the dashboard and a plain query disagree
-- ===========================================
--
-- The console's two views are:
--
--   GXO's list          GXO, not resolved, and not blocked behind another
--                       shipment's stock. ko_fixed is NOT part of it — a
--                       fixed shipment stays on GXO's list until its
--                       documents are printed.
--
--   Everything open     ko_fixed = false, not resolved, same stock rule, at
--                       every warehouse, GXO or not.
--
-- So `WHERE location_provider = 'GXO' AND ko_fixed = false` differs from
-- GXO's list in both directions: it keeps the resolved and stock-reserved
-- rows GXO's list drops, and it drops the fixed-but-not-printed rows GXO's
-- list keeps. The first query below reconciles the two.

WITH rows_ AS (
  SELECT
    IFNULL(ko_fixed, FALSE)                                       AS fixed,
    IFNULL(ko_resolved, FALSE)                                    AS resolved,
    TRIM(IFNULL(blocking_reason, '')) LIKE
      'Stock still reserved to the cancelled shipment%'            AS stock_reserved
  FROM `tlg-business-intelligence-prd.til.ko_operational_table`
  WHERE UPPER(IFNULL(location_provider, '')) = 'GXO'
)

SELECT
  COUNTIF(NOT fixed)                                        AS your_query,
  COUNTIF(NOT fixed AND resolved)                           AS hidden_resolved,
  COUNTIF(NOT fixed AND NOT resolved AND stock_reserved)    AS hidden_stock_reserved,
  COUNTIF(fixed AND NOT resolved AND NOT stock_reserved)    AS extra_fixed_not_printed,
  COUNTIF(NOT resolved AND NOT stock_reserved)              AS on_gxos_list
FROM rows_;
-- on_gxos_list = your_query - hidden_resolved - hidden_stock_reserved
--                + extra_fixed_not_printed

-- ---------------------------------------------------------------------------
-- GXO's list, exactly as the dashboard builds it
-- ---------------------------------------------------------------------------
-- No condition on ko_fixed, and the GXO test accepts a blank
-- location_provider whose source names GXO, the same fallback the workflow
-- uses.
SELECT COUNT(*) AS on_gxos_list
FROM `tlg-business-intelligence-prd.til.ko_operational_table`
WHERE NOT IFNULL(ko_resolved, FALSE)
  AND TRIM(IFNULL(blocking_reason, '')) NOT LIKE
      'Stock still reserved to the cancelled shipment%'
  AND (UPPER(IFNULL(location_provider, '')) = 'GXO'
       OR STRPOS(UPPER(IFNULL(source, '')), 'GXO') > 0);

-- ---------------------------------------------------------------------------
-- Everything open, exactly as the dashboard builds it
-- ---------------------------------------------------------------------------
SELECT COUNT(*) AS everything_open
FROM `tlg-business-intelligence-prd.til.ko_operational_table`
WHERE NOT IFNULL(ko_resolved, FALSE)
  AND TRIM(IFNULL(blocking_reason, '')) NOT LIKE
      'Stock still reserved to the cancelled shipment%'
  AND NOT IFNULL(ko_fixed, FALSE);

-- ---------------------------------------------------------------------------
-- The GXO rows neither view shows, one by one
-- ---------------------------------------------------------------------------
SELECT
  shipment_order_number,
  source,
  ko_fixed,
  ko_resolved,
  last_tried_by_gxo_date,
  in_latest_gxo_input,
  blocking_reason,
  ko_status
FROM `tlg-business-intelligence-prd.til.ko_operational_table`
WHERE UPPER(IFNULL(location_provider, '')) = 'GXO'
  AND (IFNULL(ko_resolved, FALSE)
       OR TRIM(IFNULL(blocking_reason, '')) LIKE
          'Stock still reserved to the cancelled shipment%')
ORDER BY shipment_order_number;
