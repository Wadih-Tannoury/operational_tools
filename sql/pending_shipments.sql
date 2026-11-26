WITH a AS (
  SELECT * FROM `tlg-business-intelligence-prd.til.b2c_pending_shipments_analysis` WHERE brand != 'HB'
),
stus AS (
  SELECT shipment_order_number, data_inizio_prel, data_fine_prel, peso_oc, colli_oc, pezzi_evasi
  FROM `tlg-wlfs-prd.monitoring.gxo_stus`
  QUALIFY ROW_NUMBER() OVER (PARTITION BY shipment_order_number ORDER BY data_interfaccia DESC) = 1
),
werr AS (
  SELECT shipmentOrderNumber, errorMessage
  FROM `tlg-wlfs-prd.til.prd_shipmentStatus`
  WHERE errorMessage IS NOT NULL AND LOWER(errorMessage) NOT IN ('', 'null')
  QUALIFY ROW_NUMBER() OVER (PARTITION BY shipmentOrderNumber ORDER BY eventTimestamp DESC) = 1
),
req AS (
  SELECT shipment_order_number, SUM(SAFE_CAST(JSON_VALUE(i,'$.quantity') AS INT64)) AS requested
  FROM (
    SELECT shipment_order_number, shipment_json
    FROM `tlg-business-intelligence-prd.til.oms_active_shipments`
    WHERE shipmentStatus='READY'
    QUALIFY ROW_NUMBER() OVER (PARTITION BY shipment_order_number ORDER BY order_submit_date DESC) = 1
  ), UNNEST(JSON_QUERY_ARRAY(shipment_json,'$.items')) i
  GROUP BY 1
),
refunds AS (
  SELECT DISTINCT JSON_VALUE(requestPayload,'$.data.refundResult.orderNumber') AS orderNumber
  FROM `tlg-wlfs-prd.til.prd_tilEvents`
  WHERE eventType='REFUND_RESULT'
    AND REGEXP_CONTAINS(JSON_VALUE(requestPayload,'$.data.refundResult.documentNumber'), r'^(CRN-|CRNUE-|CUS-|RCORR26-)')
),
prev AS (
  SELECT shipment_order_number,
         REGEXP_EXTRACT(TO_JSON_STRING(shipment_json), r'"previousOrderId":"([^"]+)"') AS previousOrderId
  FROM `tlg-business-intelligence-prd.til.oms_active_shipments` WHERE shipmentStatus='READY'
  QUALIFY ROW_NUMBER() OVER (PARTITION BY shipment_order_number ORDER BY order_submit_date DESC) = 1
),
-- OMS logistic order number (the real GXO/WMS order reference, e.g. 'FE188817' = siteDomain + omsShipmentNumber),
-- parsed from the shipmentCreation event into til.prd_orders. It is NOT in oms_active_shipments.shipment_json
-- (0/10.675 READY shipments carry the key) — the old source — which is why logord used to fall back to the
-- shipment_order_number for EVERY order, making the two columns look identical. prd_orders is europe-west1
-- (same region as this query) and logisticOrderNumber is never null there; key is not unique -> dedup latest.
ord AS (
  SELECT shipmentOrderNumber, logisticOrderNumber
  FROM `tlg-wlfs-prd.til.prd_orders`
  QUALIFY ROW_NUMBER() OVER (PARTITION BY shipmentOrderNumber ORDER BY timestamp DESC) = 1
),
zpbase AS (
  -- short-pick lines from the canonical view (display-only, "Short Picks" monitoring tab).
  SELECT shipmentOrderNumber AS son, productId, line_status, shipment_class,
         provvisorio, phantom, cancellato_da_noi, ancora_aperto_gxo,
         requested_qty, fullstock_sellable_pre_assign, assign_date
  FROM `tlg-wlfs-prd.monitoring.gxo_zero_pick_detail`
  WHERE shipment_class LIKE 'SHORT_PICK%'
),
sku_recid AS (
  -- recidivo: how many distinct short-pick shipments each SKU went to 0 on (view window)
  SELECT productId, COUNT(DISTINCT son) AS n_short
  FROM zpbase WHERE line_status='ZERO' GROUP BY productId
),
zp AS (
  -- aggregated to one row per shipment; only SHORT_PICK_* shipments carry a value.
  SELECT b.son,
    ANY_VALUE(b.shipment_class) AS shipment_class,
    MAX(b.provvisorio) AS provvisorio,
    COUNTIF(b.line_status='ZERO') AS zero_lines,
    MAX(b.phantom) AS any_phantom,
    MAX(b.phantom AND b.cancellato_da_noi AND b.ancora_aperto_gxo) AS cause_a,
    MAX(b.ancora_aperto_gxo) AS ancora_aperto,
    SUM(IF(b.line_status='ZERO', IFNULL(b.fullstock_sellable_pre_assign,0), 0)) AS zero_declared_sellable,
    SUM(IF(b.line_status='ZERO', b.requested_qty, 0)) AS missing_qty,
    STRING_AGG(DISTINCT CASE WHEN b.line_status='ZERO' THEN b.productId END, ', ') AS zero_skus,
    MAX(CASE WHEN b.line_status='ZERO' THEN r.n_short END) AS sku_recid_max,
    MAX(b.assign_date) AS assign_date
  FROM zpbase b
  LEFT JOIN sku_recid r ON r.productId = b.productId
  GROUP BY b.son
),
zp_esito AS (
  -- OMS reaction to the short pick (brain br-807b3510d53d): itemsdeclined -> re-split onto a
  -- new shipment (retry); itemscanceled -> line cancelled, no retry.
  SELECT shipmentOrderNumber AS son,
    LOGICAL_OR(eventType='shipment.itemsdeclined')  AS resplit,
    LOGICAL_OR(eventType='shipment.itemscanceled')  AS line_cancelled
  FROM `tlg-wlfs-prd.til.prd_tilEvents`
  WHERE eventTimestamp >= TIMESTAMP('2026-10-25')
    AND eventType IN ('shipment.itemsdeclined','shipment.itemscanceled')
    AND shipmentOrderNumber IS NOT NULL
  GROUP BY son
)
SELECT
  a.pending_days                                        AS days,
  IFNULL(a.brand, '')                                   AS brand,
  CASE
    WHEN IFNULL(a.channels, '') NOT IN ('', 'ecommerce')                           THEN a.channels
    WHEN a.shipment_order_number LIKE '%-FF-%' OR a.shipment_order_number LIKE '%-FF%' THEN 'Farfetch'
    WHEN a.shipment_order_number LIKE '%-IMAP%'                                     THEN 'IntraMirror'
    WHEN a.shipment_order_number LIKE '%-CT%'                                       THEN 'Cettire'
    WHEN a.shipment_order_number LIKE '%-MT%'                                       THEN 'Miinto'
    WHEN a.shipment_order_number LIKE '%-ZL%'                                       THEN 'Zalando'
    WHEN a.shipment_order_number LIKE '%-VP%' OR a.shipment_order_number LIKE '%-VV%' THEN 'Veepee'
    ELSE IFNULL(a.channels, '')
  END                                                    AS ch,
  IFNULL(a.external_order_id, '')                       AS id,
  IFNULL(a.shipment_order_number, '')                   AS ship,
  IFNULL(a.activeTaskName, '')                          AS task,
  IFNULL(FORMAT_TIMESTAMP('%Y-%m-%d', a.shipment_creation_date), '') AS created,
  IFNULL(a.originWMS, '')                               AS wms,
  IFNULL(a.fulfillmentLocationCode, '')                 AS flc,
  IFNULL(a.wh_last_event_type, '')                      AS ev,
  CASE WHEN a.is_ko IS NULL THEN '' WHEN a.is_ko THEN '1' ELSE '0' END                         AS ko,
  CASE WHEN a.invoice_result IS NULL THEN '' WHEN a.invoice_result THEN '1' ELSE '0' END       AS inv,
  CASE WHEN a.payment_result IS NULL THEN '' WHEN a.payment_result THEN '1' ELSE '0' END       AS pay,
  CASE WHEN a.shipmentLabelSucceeded IS NULL THEN '' WHEN a.shipmentLabelSucceeded THEN '1' ELSE '0' END AS lbl,
  IFNULL(a.shippingMethod, '')                          AS method,
  IFNULL(a.countryCode, '')                             AS cc,
  IFNULL(a.processorId, '')                             AS psp,
  IFNULL(a.currencyCode, '')                            AS cur,
  IFNULL(a.stus_stato, '')                              AS stus,
  IFNULL(a.action, '')                                  AS action,
  IFNULL(FORMAT_TIMESTAMP('%Y-%m-%d', a.order_submit_date), '') AS submit,
  CAST(IFNULL(a.lineItemTotal, 0) AS FLOAT64)           AS tot,
  CAST(IFNULL(a.is_vip, FALSE) AS INT64)                AS vip,
  IFNULL(a.sourceCompany, '')                           AS srcCo,
  IFNULL(COALESCE(a.gxo_ko_reason, a.shipment_notes_label), '') AS err,
  IFNULL(a.locationType, '')                            AS ltype,
  IFNULL(a.shipmentType, '')                            AS stype,
  IFNULL(a.isGift, '')                                  AS gift,
  IFNULL(a.shipmentCarrierCode, '')                     AS carrier,
  IFNULL(a.shipmentLabelErrorDetails, '')               AS lblerr,
  IFNULL(a.productIds, '')                              AS productIds,
  IFNULL(a.logisticsProductTypes, '')                   AS types,
  -- ── NEW feedback columns (C7/C8/C6) ──
  IFNULL(a.returnLabelErrorDetails, '')                 AS rlblerr,
  -- picked / requested as TWO separate values. picked comes from the PICKED WMS event
  -- (GEODIS/GXO/MODA only) -> NULL for stores, which never emit it; show empty, not 0.
  IFNULL(CAST(a.picked_quantity AS STRING), '')         AS picked,
  IFNULL(CAST(req.requested AS STRING), '')             AS requested,
  CASE WHEN a.picked_quantity IS NOT NULL AND req.requested IS NOT NULL AND a.picked_quantity < req.requested THEN '1' ELSE '0' END AS prPartial,
  IFNULL(FORMAT_TIMESTAMP('%Y-%m-%d %H:%M', stus.data_inizio_prel), '') AS pickStart,
  IFNULL(FORMAT_TIMESTAMP('%Y-%m-%d %H:%M', stus.data_fine_prel), '')   AS pickEnd,
  IFNULL(CAST(stus.peso_oc AS STRING), '')              AS peso,
  IFNULL(CAST(stus.colli_oc AS STRING), '')             AS colli,
  IFNULL(CAST(stus.pezzi_evasi AS STRING), '')          AS pezzi,
  IFNULL(werr.errorMessage, '')                         AS wmsErr,
  CASE WHEN a.is_exchange THEN '1' ELSE '0' END         AS exch,
  CASE WHEN COALESCE(a.is_exchange,FALSE) AND r.orderNumber IS NOT NULL THEN '1' ELSE '0' END AS prevRef,
  IFNULL(prev.previousOrderId, '')                      AS prevOrd,
  IFNULL(ord.logisticOrderNumber, a.shipment_order_number) AS logord,
  -- BOPIS/TRANSFER shipment pair (all four now present in the analysis table)
  IFNULL(a.first_shipment, '')                          AS f1,
  IFNULL(a.first_shipment_type, '')                     AS f1t,
  IFNULL(a.first_fulfillment_location_code, '')         AS f1l,
  IFNULL(a.first_active_task_name, '')                  AS f1a,
  IFNULL(a.second_shipment, '')                         AS s2,
  IFNULL(a.second_shipment_type, '')                    AS s2t,
  IFNULL(a.second_fulfillment_location_code, '')        AS s2l,
  IFNULL(a.second_active_task_name, '')                 AS s2a,
  IFNULL(a.second_shipment_latest_wh_event_type, '')    AS s2ev,
  IFNULL(FORMAT_TIMESTAMP('%Y-%m-%d %H:%M', a.second_shipment_latest_wh_event_timestamp), '') AS s2ts,
  -- tracking / carrier / stus / KO / docs — the rest of b2c_pending_shipments_analysis (for the full detail)
  IFNULL(a.shipmentTrackingNumber, '')                  AS trk,
  IFNULL(a.shipmentCarrierCode, '')                     AS carrier2,
  IFNULL(a.returnTrackingNumber, '')                    AS rtrk,
  IFNULL(a.returnCarrierCode, '')                       AS rcarrier,
  IFNULL(a.stus_stato, '')                              AS stusStato,
  IFNULL(a.stus_tracking, '')                           AS stusTrk,
  CASE WHEN a.is_ko IS NULL THEN '' WHEN a.is_ko THEN '1' ELSE '0' END AS ko2,
  IFNULL(a.gxo_ko_reason, '')                           AS koReason,
  CASE WHEN a.invoice_result IS NULL THEN '' WHEN a.invoice_result THEN '1' ELSE '0' END AS inv2,
  CASE WHEN a.payment_result IS NULL THEN '' WHEN a.payment_result THEN '1' ELSE '0' END AS pay2,
  CASE WHEN a.shipmentLabelSucceeded IS NULL THEN '' WHEN a.shipmentLabelSucceeded THEN '1' ELSE '0' END AS lbl2,
  IFNULL(a.processorId, '')                             AS psp2,
  IFNULL(CAST(a.oms_shipment_number AS STRING), '')     AS oms,
  IFNULL(FORMAT_TIMESTAMP('%Y-%m-%d', a.shipment_creation_effective), '') AS createdEff,
  IFNULL(FORMAT_TIMESTAMP('%Y-%m-%d %H:%M', a.wh_last_event_timestamp), '') AS whTs,
  IFNULL(FORMAT_TIMESTAMP('%Y-%m-%d %H:%M', a.picked_event_timestamp), '') AS pickedTs,
  IFNULL(CAST(a.isPersonalized AS STRING), '')          AS personalized,
  IFNULL(a.wh_timezone, '')                             AS tz,
  IFNULL(a.shipment_notes_invoice, '')                  AS notesInv,
  IFNULL(a.shipment_notes_label, '')                    AS notesLbl,
  IFNULL(a.shipment_notes_returnlabel, '')              AS notesRet,
  -- ── short-pick / phantom (view gxo_zero_pick_detail; display-only "Pick a 0" monitoring tab) ──
  CASE zp.shipment_class WHEN 'SHORT_PICK_WHOLE' THEN 'Whole' WHEN 'SHORT_PICK_PARTIAL' THEN 'Partial' ELSE '' END AS sp,
  CASE WHEN zp.cause_a THEN 'A' WHEN zp.any_phantom THEN 'B' WHEN zp.zero_lines > 0 THEN 'C' ELSE '' END AS spc,
  IFNULL(CAST(zp.zero_lines AS STRING), '')             AS spZero,
  IFNULL(CAST(zp.zero_declared_sellable AS STRING), '') AS spDecl,
  IFNULL(zp.zero_skus, '')                              AS spSku,
  IFNULL(CAST(zp.missing_qty AS STRING), '')            AS spMiss,
  IFNULL(CAST(zp.sku_recid_max AS STRING), '')          AS spRecid,
  IFNULL(FORMAT_DATE('%Y-%m-%d', zp.assign_date), '')   AS spDay,
  CASE
    WHEN ze.resplit THEN 'Re-split'
    WHEN ze.line_cancelled THEN 'Cancelled'
    WHEN zp.ancora_aperto THEN 'Still open'
    ELSE ''
  END                                                   AS spEsito,
  CASE WHEN zp.son IS NULL THEN '' WHEN zp.provvisorio THEN '1' ELSE '0' END AS spProv
FROM a
LEFT JOIN stus    ON stus.shipment_order_number = a.shipment_order_number
LEFT JOIN werr    ON werr.shipmentOrderNumber    = a.shipment_order_number
LEFT JOIN req     ON req.shipment_order_number   = a.shipment_order_number
LEFT JOIN prev    ON prev.shipment_order_number  = a.shipment_order_number
LEFT JOIN ord     ON ord.shipmentOrderNumber     = a.shipment_order_number
LEFT JOIN refunds r ON r.orderNumber = prev.previousOrderId
LEFT JOIN zp       ON zp.son = a.shipment_order_number
LEFT JOIN zp_esito ze ON ze.son = a.shipment_order_number
