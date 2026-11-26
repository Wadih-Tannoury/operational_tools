-- The pending list: the shipments this workflow is about
-- ======================================================
--
-- One table, one filter. The actions that belong to this workflow are the
-- TECH ones and the back-office ones, less the single GXO realignment case
-- that is somebody else's to chase:
--
--     action LIKE '%TECH%' OR action LIKE 'BO - %'
--     AND action <> 'TECH - GXO_MISSED_STOCK_EXIT - GXO TO REALIGN'
--     AND brand <> 'HB'
--
-- That filter is the whole definition of the list, and it replaces the much
-- larger dashboard query that used to stand here — the one that joined
-- gxo_stus, prd_shipmentStatus, prd_tilEvents, oms_active_shipments,
-- prd_orders and gxo_zero_pick_detail to build the b2c dashboard's display
-- columns. None of that enrichment is read by this workflow, so none of it
-- is paid for here.
--
-- The columns below are the ones the run actually uses: the shipment and the
-- things that decide what happens to it. They are aliased to the same short
-- names the dashboard query used, so pending_query.py maps them unchanged.
--
-- A NULL action is excluded by the filter, as it must be: an unclassified
-- shipment has not been triaged into this workflow. A NULL brand is excluded
-- too, by the same SQL rule — `brand <> 'HB'` is unknown rather than true
-- when brand is NULL. That matches what the dashboard query did before it.
SELECT
  IFNULL(shipment_order_number, '')                                  AS ship,
  IFNULL(CAST(oms_shipment_number AS STRING), '')                    AS oms,
  IFNULL(external_order_id, '')                                      AS id,
  IFNULL(action, '')                                                 AS action,
  IFNULL(activeTaskName, '')                                         AS task,
  IFNULL(brand, '')                                                  AS brand,
  -- The channel as the dashboard derives it: the column when it says
  -- something useful, and the marketplace read off the shipment number when
  -- it does not. The OERODP is built from the channel's first letter, so a
  -- blank here would quietly change it.
  CASE
    WHEN IFNULL(channels, '') NOT IN ('', 'ecommerce')                     THEN channels
    WHEN shipment_order_number LIKE '%-FF-%'
      OR shipment_order_number LIKE '%-FF%'                                THEN 'Farfetch'
    WHEN shipment_order_number LIKE '%-IMAP%'                              THEN 'IntraMirror'
    WHEN shipment_order_number LIKE '%-CT%'                                THEN 'Cettire'
    WHEN shipment_order_number LIKE '%-MT%'                                THEN 'Miinto'
    WHEN shipment_order_number LIKE '%-ZL%'                                THEN 'Zalando'
    WHEN shipment_order_number LIKE '%-VP%'
      OR shipment_order_number LIKE '%-VV%'                                THEN 'Veepee'
    ELSE IFNULL(channels, '')
  END                                                                AS ch,
  IFNULL(pending_days, 0)                                            AS days,
  IFNULL(FORMAT_TIMESTAMP('%Y-%m-%d', order_submit_date), '')        AS submit,
  IFNULL(FORMAT_TIMESTAMP('%Y-%m-%d', shipment_creation_date), '')   AS created,
  IFNULL(FORMAT_TIMESTAMP('%Y-%m-%d', shipment_creation_effective), '') AS createdEff,
  IFNULL(fulfillmentLocationCode, '')                                AS flc,
  IFNULL(originWMS, '')                                              AS wms,
  IFNULL(wh_last_event_type, '')                                     AS ev,
  IFNULL(stus_stato, '')                                             AS stus,
  IFNULL(gxo_ko_reason, '')                                          AS koReason,
  IFNULL(shipmentLabelErrorDetails, '')                              AS lblerr,
  IFNULL(returnLabelErrorDetails, '')                                AS rlblerr,
  CASE WHEN shipmentLabelSucceeded IS NULL THEN ''
       WHEN shipmentLabelSucceeded THEN '1' ELSE '0' END             AS lbl,
  IFNULL(shipmentTrackingNumber, '')                                 AS trk,
  IFNULL(returnTrackingNumber, '')                                   AS rtrk,
  IFNULL(shipmentCarrierCode, '')                                    AS carrier,
  IFNULL(shippingMethod, '')                                         AS method,
  IFNULL(countryCode, '')                                            AS cc
FROM `tlg-business-intelligence-prd.til.b2c_pending_shipments_analysis`
WHERE (action LIKE '%TECH%' OR action LIKE 'BO - %')
  AND action <> 'TECH - GXO_MISSED_STOCK_EXIT - GXO TO REALIGN'
  AND brand <> 'HB'
