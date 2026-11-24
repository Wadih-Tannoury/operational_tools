-- Backfill reason_code and more_info on the cancellation ledger
-- =============================================================
--
--   `tlg-business-intelligence-prd`.til.warehouse_orders_cancellation_table
--
-- from the "Orders to cancel" workbook. Run the updated
-- sql/warehouse_orders_cancellation_table.sql first, so the two columns exist.
--
-- THE MATCH
-- order_number from the workbook against shipment_order_number in the ledger,
-- with **the 6th character of both ignored** — the two systems differ there
-- and nowhere else, so 'BB-EUB00072383' matches 'BB-EUA00072383'. Both sides
-- are trimmed and upper-cased first.
--
-- WHAT IS IN HERE
-- 176 match keys, out of the 192 the workbook produces. Several workbook
-- rows can share one key — 'OW-USA00039548' and 'OW-US-00039548' are the same
-- shipment once the 6th character goes — so each key appears once, with the
-- reason its spellings agree on. The order numbers behind each key are in the
-- comment beside it. Left out, deliberately:
--
--   * 11 with no usable reason in the workbook:
--   LN-CT-1929143761-4
--   LN-CT-1929239239-4
--   LN-CT-1929269164-4
--   LN-CT-1929479965-4
--   LN-CT-1929713001-4
--   LN-CT-1929779766-4
--   LN-CT-1929926546-4
--   LN-LX-00018513
--   LN-MT-NODE534B4ADBD2
--   LN-NA-00001724
--   LN-NA-00001725
--
--   * 5 where the spellings of one key disagree about the reason. The query
--     cannot choose; set these by hand:
--   HE-EU00136490  (HE-EU-00136490, HE-EUA00136490): Customer Changed Their Mind / Need To Change Payment Method
--   HE-EU00137477  (HE-EUB00137477): Customer Changed Their Mind / Need To Change Payment Method
--   HE-EU00137842  (HE-EU-00137842, HE-EUB00137842): Customer Changed Their Mind / Need To Change Payment Method
--   HE-EU00140132  (HE-EUA00140132): Customer Changed Their Mind / Need To Change Payment Method
--   WF-EU00013763  (WF-EU-00013763): Customer Changed Their Mind / Other
--
-- TWO REASONS WERE REWRITTEN to values Kibo accepts, as the workbook spells
-- them its own way. Change them here if you would rather they went elsewhere:
--
--   'Other - Chargeback opened'  ->  reason_code 'Other', more_info 'Chargeback opened'
--   'New payment requested'      ->  reason_code 'Other', more_info 'New payment requested'
--
-- 'Other' with nothing beside it carries more_info 'Other' — the rule the
-- console and the workers follow, because an empty moreInfo on 'Other' tells
-- Kibo nothing at all.


-- ---------------------------------------------------------------------------
-- 1. Look before you write. Nothing is changed by this statement.
-- ---------------------------------------------------------------------------

WITH source AS (
  SELECT * FROM UNNEST([
    STRUCT<match_key STRING, reason_code STRING, more_info STRING>
    ('BB-EU00071965', 'No Inventory', ''),  -- BB-EU-00071965
    ('BB-EU00072383', 'No Inventory', ''),  -- BB-EUB00072383
    ('BB-MTUK81FF2499CD55', 'Customer Changed Their Mind', ''),  -- BB-MT-UK81FF2499CD55
    ('CV-EU00084535', 'Customer Changed Their Mind', ''),  -- CV-EUA00084535
    ('CV-MTDEC5AC696D7B54', 'Customer Changed Their Mind', ''),  -- CV-MT-DEC5AC696D7B54
    ('CV-US00044668', 'No Inventory', ''),  -- CV-US-00044668
    ('DG-EU01683898', 'Other', 'Chargeback opened'),  -- DG-EU-01683898
    ('DG-EU01685677', 'Need To Change Payment Method', ''),  -- DG-EU-01685677
    ('DG-EU01685915', 'Need To Change Payment Method', ''),  -- DG-EU-01685915
    ('DG-FFDWH191116899', 'Customer Changed Their Mind', ''),  -- DG-FF-DWH191116899
    ('DG-FFDWH191129109', 'Customer Changed Their Mind', ''),  -- DG-FF-DWH191129109
    ('DJ-US00032514', 'Customer Changed Their Mind', ''),  -- DJ-USD00032514
    ('FE-EU00134101', 'Customer Changed Their Mind', ''),  -- FE-EU-00134101
    ('FE-US00057576', 'Customer Changed Their Mind', ''),  -- FE-US-00057576
    ('HE-EU00137568', 'Need To Change Payment Method', ''),  -- HE-EU-00137568
    ('HE-EU00137635', 'No Inventory', ''),  -- HE-EU-00137635
    ('HE-EU00137821', 'Customer Changed Their Mind', ''),  -- HE-EU-00137821
    ('HE-EU00137876', 'Other', 'New payment requested'),  -- HE-EU-00137876
    ('HE-FFTH5190996894', 'Customer Changed Their Mind', ''),  -- HE-FF-TH5190996894
    ('HE-FFTH5191143208', 'Customer Changed Their Mind', ''),  -- HE-FF-TH5191143208
    ('LN-CT1223274516-5', 'Customer Changed Their Mind', ''),  -- LN-CT-1223274516-5
    ('LN-CT1242350553-6', 'Customer Changed Their Mind', ''),  -- LN-CT-1242350553-6
    ('LN-CT1282354731-8', 'Customer Changed Their Mind', ''),  -- LN-CT-1282354731-8
    ('LN-CT1929002956-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929002956-4
    ('LN-CT1929010371-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929010371-4
    ('LN-CT1929017626-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929017626-4
    ('LN-CT1929043018-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929043018-4
    ('LN-CT1929048743-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929048743-4
    ('LN-CT1929052355-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929052355-4
    ('LN-CT1929055190-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929055190-4
    ('LN-CT1929075002-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929075002-4
    ('LN-CT1929085801-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929085801-4
    ('LN-CT1929088062-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929088062-4
    ('LN-CT1929107525-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929107525-4
    ('LN-CT1929109303-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929109303-4
    ('LN-CT1929127033-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929127033-4
    ('LN-CT1929145572-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929145572-4
    ('LN-CT1929149600-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929149600-4
    ('LN-CT1929158240-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929158240-4
    ('LN-CT1929170200-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929170200-4
    ('LN-CT1929171346-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929171346-4
    ('LN-CT1929177993-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929177993-4
    ('LN-CT1929188298-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929188298-4
    ('LN-CT1929192282-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929192282-4
    ('LN-CT1929212825-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929212825-4
    ('LN-CT1929213297-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929213297-4
    ('LN-CT1929222400-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929222400-4
    ('LN-CT1929223861-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929223861-4
    ('LN-CT1929245001-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929245001-4
    ('LN-CT1929245157-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929245157-4
    ('LN-CT1929247586-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929247586-4
    ('LN-CT1929247654-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929247654-4
    ('LN-CT1929265496-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929265496-4
    ('LN-CT1929275082-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929275082-4
    ('LN-CT1929285016-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929285016-4
    ('LN-CT1929292025-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929292025-4
    ('LN-CT1929295174-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929295174-4
    ('LN-CT1929307730-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929307730-4
    ('LN-CT1929322221-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929322221-4
    ('LN-CT1929335089-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929335089-4
    ('LN-CT1929340322-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929340322-4
    ('LN-CT1929363866-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929363866-4
    ('LN-CT1929372316-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929372316-4
    ('LN-CT1929372574-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929372574-4
    ('LN-CT1929420310-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929420310-4
    ('LN-CT1929445283-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929445283-4
    ('LN-CT1929455419-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929455419-4
    ('LN-CT1929455767-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929455767-4
    ('LN-CT1929470911-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929470911-4
    ('LN-CT1929488628-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929488628-4
    ('LN-CT1929512941-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929512941-4
    ('LN-CT1929518262-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929518262-4
    ('LN-CT1929535047-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929535047-4
    ('LN-CT1929562014-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929562014-4
    ('LN-CT1929580519-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929580519-4
    ('LN-CT1929592264-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929592264-4
    ('LN-CT1929593096-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929593096-4
    ('LN-CT1929618342-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929618342-4
    ('LN-CT1929625137-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929625137-4
    ('LN-CT1929628950-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929628950-4
    ('LN-CT1929703213-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929703213-4
    ('LN-CT1929708757-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929708757-4
    ('LN-CT1929732855-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929732855-4
    ('LN-CT1929732990-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929732990-4
    ('LN-CT1929733632-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929733632-4
    ('LN-CT1929807364-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929807364-4
    ('LN-CT1929808174-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929808174-4
    ('LN-CT1929840462-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929840462-4
    ('LN-CT1929848493-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929848493-4
    ('LN-CT1929873828-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929873828-4
    ('LN-CT1929875224-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929875224-4
    ('LN-CT1929877956-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929877956-4
    ('LN-CT1929900537-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929900537-4
    ('LN-CT1929955740-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929955740-4
    ('LN-CT1929987813-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929987813-4
    ('LN-CT1929999344-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929999344-4
    ('LN-EU01695382', 'Customer Changed Their Mind', ''),  -- LN-EU-01695382
    ('LN-IMP26110694363', 'Customer Changed Their Mind', ''),  -- LN-IM-P26110694363
    ('LN-IMP26110797936', 'Customer Changed Their Mind', ''),  -- LN-IM-P26110797936
    ('LN-IMP26110798128', 'Customer Changed Their Mind', ''),  -- LN-IM-P26110798128
    ('LN-IMP26110798129', 'Customer Changed Their Mind', ''),  -- LN-IM-P26110798129
    ('LN-IMP26110800106', 'Customer Changed Their Mind', ''),  -- LN-IM-P26110800106
    ('LN-IMP26110800616', 'Customer Changed Their Mind', ''),  -- LN-IM-P26110800616
    ('LN-IMP26110800617', 'Customer Changed Their Mind', ''),  -- LN-IM-P26110800617
    ('LN-IMP26110800618', 'Customer Changed Their Mind', ''),  -- LN-IM-P26110800618
    ('LN-IMP26110800619', 'Customer Changed Their Mind', ''),  -- LN-IM-P26110800619
    ('LN-IMP26110800620', 'Customer Changed Their Mind', ''),  -- LN-IM-P26110800620
    ('LN-IMP26110800621', 'Customer Changed Their Mind', ''),  -- LN-IM-P26110800621
    ('LN-IMP26110800622', 'Customer Changed Their Mind', ''),  -- LN-IM-P26110800622
    ('LN-IMP26110899231', 'Customer Changed Their Mind', ''),  -- LN-IM-P26110899231
    ('LN-IMP26110899294', 'Customer Changed Their Mind', ''),  -- LN-IM-P26110899294
    ('LN-IMP26110899315', 'Customer Changed Their Mind', ''),  -- LN-IM-P26110899315
    ('LN-IMP26110906541', 'Customer Changed Their Mind', ''),  -- LN-IM-P26110906541
    ('LN-IMP26111010092', 'Customer Changed Their Mind', ''),  -- LN-IM-P26111010092
    ('LN-IMP26111010888', 'Customer Changed Their Mind', ''),  -- LN-IM-P26111010888
    ('LN-IMP26111013377', 'Customer Changed Their Mind', ''),  -- LN-IM-P26111013377
    ('LN-IMP26111013453', 'Customer Changed Their Mind', ''),  -- LN-IM-P26111013453
    ('LN-IMP26111115134', 'Customer Changed Their Mind', ''),  -- LN-IM-P26111115134
    ('LN-IMP26111115709', 'Customer Changed Their Mind', ''),  -- LN-IM-P26111115709
    ('LN-IMP26111116473', 'Customer Changed Their Mind', ''),  -- LN-IM-P26111116473
    ('LN-IMP26111118872', 'Customer Changed Their Mind', ''),  -- LN-IM-P26111118872
    ('LN-IMP26111325800', 'Customer Changed Their Mind', ''),  -- LN-IM-P26111325800
    ('LN-IMP26111326418', 'Customer Changed Their Mind', ''),  -- LN-IM-P26111326418
    ('LN-IMP26111432708', 'Customer Changed Their Mind', ''),  -- LN-IM-P26111432708
    ('LN-IMP26111638486', 'Customer Changed Their Mind', ''),  -- LN-IM-P26111638486
    ('LN-NA00001641', 'Customer Changed Their Mind', ''),  -- LN-NA-00001641
    ('MA-EU00011828', 'Customer Changed Their Mind', ''),  -- MA-EUA00011828
    ('OW-EU00047483', 'Customer Changed Their Mind', ''),  -- OW-EU-00047483
    ('OW-EU00048044', 'Item Not Available', ''),  -- OW-EU-00048044
    ('OW-EU00049166', 'Item Not Available', ''),  -- OW-EU-00049166
    ('OW-EU00049848', 'No Inventory', ''),  -- OW-EU-00049848
    ('OW-FFOW7191109547', 'Customer Changed Their Mind', ''),  -- OW-FF-OW7191109547
    ('OW-FFOW7191153538', 'Customer Changed Their Mind', ''),  -- OW-FF-OW7191153538
    ('OW-US00038449', 'No Inventory', ''),  -- OW-US-00038449
    ('OW-US00039497', 'Customer Changed Their Mind', ''),  -- OW-US-00039497
    ('OW-US00039548', 'Other', 'Other'),  -- OW-US-00039548, OW-USA00039548
    ('PA-EU00019755', 'Need To Change Payment Method', ''),  -- PA-EU-00019755
    ('PJ-FFTGP191112973', 'Customer Changed Their Mind', ''),  -- PJ-FF-TGP191112973
    ('PJ-FFTGP191131034', 'Customer Changed Their Mind', ''),  -- PJ-FF-TGP191131034
    ('PJ-FFTGP191136162', 'Customer Changed Their Mind', ''),  -- PJ-FF-TGP191136162
    ('PL-MTBEFF54B2767184', 'Customer Changed Their Mind', ''),  -- PL-MT-BEFF54B2767184
    ('RC-EU00014332', 'Customer Changed Their Mind', ''),  -- RC-EUA00014332
    ('RC-US00014165', 'Customer Changed Their Mind', ''),  -- RC-US-00014165
    ('SC-EU01299882', 'No Inventory', ''),  -- SC-EU-01299882
    ('SC-EU01301277', 'No Inventory', ''),  -- SC-EU-01301277
    ('SC-FFTGA190998425', 'Customer Changed Their Mind', ''),  -- SC-FF-TGA190998425
    ('SC-FFTGA191015502', 'Customer Changed Their Mind', ''),  -- SC-FF-TGA191015502
    ('SC-MTNO8C655B128BC6', 'Item Not Available', ''),  -- SC-MT-NO8C655B128BC6
    ('SC-MTPL72FF8F315102', 'Other', 'Other'),  -- SC-MT-PL72ff8f315102
    ('SC-US00009255', 'Other', 'Other'),  -- SC-US-00009255
    ('SC-US00009368', 'Other', 'Other'),  -- SC-US-00009368
    ('SC-US00009377', 'Other', 'Other'),  -- SC-US-00009377
    ('SC-US00009597', 'Other', 'Other'),  -- SC-US-00009597
    ('SW-EU00828299', 'No Inventory', ''),  -- SW-EUG00828299
    ('SW-FFSW5191135828', 'Customer Changed Their Mind', ''),  -- SW-FF-SW5191135828
    ('SW-FFSW5191157718', 'Customer Changed Their Mind', ''),  -- SW-FF-SW5191157718
    ('WF-EU00011993', 'Customer Changed Their Mind', ''),  -- WF-EUB00011993
    ('WF-FFT3W191126708', 'Customer Changed Their Mind', ''),  -- WF-FF-T3W191126708
    ('WF-MK032805-3118708', 'No Inventory', ''),  -- WF-MK-032805-3118708
    ('WF-MTBE12D28C726265', 'Customer Changed Their Mind', ''),  -- WF-MT-BE12D28C726265
    ('WF-MTNO393664E8A721', 'Customer Changed Their Mind', ''),  -- WF-MT-NO393664E8A721
    ('WF-VP26024487', 'No Inventory', ''),  -- WF-VP-26024487
    ('WF-VP26075669', 'Other', 'Other'),  -- WF-VP-26075669
    ('WF-VP26152211', 'Other', 'Other'),  -- WF-VP-26152211
    ('WF-VP26180473', 'No Inventory', ''),  -- WF-VP-26180473
    ('WF-ZL10103739398086', 'Customer Changed Their Mind', ''),  -- WF-ZL-10103739398086
    ('WL-CT1926990038-4', 'Customer Changed Their Mind', ''),  -- WL-CT-1926990038-4
    ('WL-CT1929995878-4', 'Customer Changed Their Mind', ''),  -- WL-CT-1929995878-4
    ('WL-EU00590746', 'Customer Changed Their Mind', ''),  -- WL-EUA00590746
    ('WL-EU00592126', 'Other', 'Other'),  -- WL-EU-00592126
    ('WL-EU00592134', 'Other', 'Other'),  -- WL-EU-00592134
    ('WL-EU00592138', 'Other', 'Other'),  -- WL-EU-00592138
    ('WL-EU00592297', 'No Inventory', ''),  -- WL-EU-00592297
    ('WL-EU00592315', 'Item Not Available', ''),  -- WL-EU-00592315
    ('WL-EU00592338', 'Other', 'Other'),  -- WL-EU-00592338
    ('WL-FFWST191049400', 'Customer Changed Their Mind', '')  -- WL-FF-WST191049400
  ])
),
ledger AS (
  SELECT
    shipment_order_number,
    reason_code AS existing_reason,
    CONCAT(SUBSTR(UPPER(TRIM(shipment_order_number)), 1, 5),
           SUBSTR(UPPER(TRIM(shipment_order_number)), 7)) AS match_key
  FROM `tlg-business-intelligence-prd.til.warehouse_orders_cancellation_table`
  WHERE shipment_order_number IS NOT NULL
    AND TRIM(shipment_order_number) != ''
)
SELECT
  s.match_key,
  s.reason_code,
  s.more_info,
  l.shipment_order_number,
  CASE
    WHEN l.shipment_order_number IS NULL THEN 'no match in the ledger'
    WHEN l.existing_reason IS NOT NULL AND TRIM(l.existing_reason) != ''
      THEN 'already has a reason, left alone'
    ELSE 'will be set'
  END AS outcome
FROM source s
LEFT JOIN ledger l USING (match_key)
ORDER BY outcome, s.match_key;


-- ---------------------------------------------------------------------------
-- 2. The update
-- ---------------------------------------------------------------------------
-- Only rows with no reason yet are touched. To overwrite reasons already
-- recorded, delete the last line of the WHERE clause.

UPDATE `tlg-business-intelligence-prd.til.warehouse_orders_cancellation_table` AS t
SET
  t.reason_code = s.reason_code,
  t.more_info = s.more_info,
  t.row_last_modified_date = CURRENT_TIMESTAMP()
FROM (
  SELECT * FROM UNNEST([
    STRUCT<match_key STRING, reason_code STRING, more_info STRING>
    ('BB-EU00071965', 'No Inventory', ''),  -- BB-EU-00071965
    ('BB-EU00072383', 'No Inventory', ''),  -- BB-EUB00072383
    ('BB-MTUK81FF2499CD55', 'Customer Changed Their Mind', ''),  -- BB-MT-UK81FF2499CD55
    ('CV-EU00084535', 'Customer Changed Their Mind', ''),  -- CV-EUA00084535
    ('CV-MTDEC5AC696D7B54', 'Customer Changed Their Mind', ''),  -- CV-MT-DEC5AC696D7B54
    ('CV-US00044668', 'No Inventory', ''),  -- CV-US-00044668
    ('DG-EU01683898', 'Other', 'Chargeback opened'),  -- DG-EU-01683898
    ('DG-EU01685677', 'Need To Change Payment Method', ''),  -- DG-EU-01685677
    ('DG-EU01685915', 'Need To Change Payment Method', ''),  -- DG-EU-01685915
    ('DG-FFDWH191116899', 'Customer Changed Their Mind', ''),  -- DG-FF-DWH191116899
    ('DG-FFDWH191129109', 'Customer Changed Their Mind', ''),  -- DG-FF-DWH191129109
    ('DJ-US00032514', 'Customer Changed Their Mind', ''),  -- DJ-USD00032514
    ('FE-EU00134101', 'Customer Changed Their Mind', ''),  -- FE-EU-00134101
    ('FE-US00057576', 'Customer Changed Their Mind', ''),  -- FE-US-00057576
    ('HE-EU00137568', 'Need To Change Payment Method', ''),  -- HE-EU-00137568
    ('HE-EU00137635', 'No Inventory', ''),  -- HE-EU-00137635
    ('HE-EU00137821', 'Customer Changed Their Mind', ''),  -- HE-EU-00137821
    ('HE-EU00137876', 'Other', 'New payment requested'),  -- HE-EU-00137876
    ('HE-FFTH5190996894', 'Customer Changed Their Mind', ''),  -- HE-FF-TH5190996894
    ('HE-FFTH5191143208', 'Customer Changed Their Mind', ''),  -- HE-FF-TH5191143208
    ('LN-CT1223274516-5', 'Customer Changed Their Mind', ''),  -- LN-CT-1223274516-5
    ('LN-CT1242350553-6', 'Customer Changed Their Mind', ''),  -- LN-CT-1242350553-6
    ('LN-CT1282354731-8', 'Customer Changed Their Mind', ''),  -- LN-CT-1282354731-8
    ('LN-CT1929002956-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929002956-4
    ('LN-CT1929010371-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929010371-4
    ('LN-CT1929017626-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929017626-4
    ('LN-CT1929043018-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929043018-4
    ('LN-CT1929048743-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929048743-4
    ('LN-CT1929052355-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929052355-4
    ('LN-CT1929055190-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929055190-4
    ('LN-CT1929075002-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929075002-4
    ('LN-CT1929085801-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929085801-4
    ('LN-CT1929088062-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929088062-4
    ('LN-CT1929107525-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929107525-4
    ('LN-CT1929109303-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929109303-4
    ('LN-CT1929127033-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929127033-4
    ('LN-CT1929145572-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929145572-4
    ('LN-CT1929149600-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929149600-4
    ('LN-CT1929158240-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929158240-4
    ('LN-CT1929170200-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929170200-4
    ('LN-CT1929171346-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929171346-4
    ('LN-CT1929177993-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929177993-4
    ('LN-CT1929188298-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929188298-4
    ('LN-CT1929192282-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929192282-4
    ('LN-CT1929212825-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929212825-4
    ('LN-CT1929213297-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929213297-4
    ('LN-CT1929222400-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929222400-4
    ('LN-CT1929223861-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929223861-4
    ('LN-CT1929245001-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929245001-4
    ('LN-CT1929245157-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929245157-4
    ('LN-CT1929247586-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929247586-4
    ('LN-CT1929247654-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929247654-4
    ('LN-CT1929265496-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929265496-4
    ('LN-CT1929275082-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929275082-4
    ('LN-CT1929285016-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929285016-4
    ('LN-CT1929292025-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929292025-4
    ('LN-CT1929295174-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929295174-4
    ('LN-CT1929307730-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929307730-4
    ('LN-CT1929322221-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929322221-4
    ('LN-CT1929335089-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929335089-4
    ('LN-CT1929340322-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929340322-4
    ('LN-CT1929363866-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929363866-4
    ('LN-CT1929372316-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929372316-4
    ('LN-CT1929372574-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929372574-4
    ('LN-CT1929420310-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929420310-4
    ('LN-CT1929445283-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929445283-4
    ('LN-CT1929455419-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929455419-4
    ('LN-CT1929455767-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929455767-4
    ('LN-CT1929470911-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929470911-4
    ('LN-CT1929488628-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929488628-4
    ('LN-CT1929512941-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929512941-4
    ('LN-CT1929518262-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929518262-4
    ('LN-CT1929535047-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929535047-4
    ('LN-CT1929562014-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929562014-4
    ('LN-CT1929580519-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929580519-4
    ('LN-CT1929592264-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929592264-4
    ('LN-CT1929593096-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929593096-4
    ('LN-CT1929618342-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929618342-4
    ('LN-CT1929625137-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929625137-4
    ('LN-CT1929628950-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929628950-4
    ('LN-CT1929703213-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929703213-4
    ('LN-CT1929708757-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929708757-4
    ('LN-CT1929732855-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929732855-4
    ('LN-CT1929732990-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929732990-4
    ('LN-CT1929733632-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929733632-4
    ('LN-CT1929807364-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929807364-4
    ('LN-CT1929808174-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929808174-4
    ('LN-CT1929840462-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929840462-4
    ('LN-CT1929848493-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929848493-4
    ('LN-CT1929873828-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929873828-4
    ('LN-CT1929875224-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929875224-4
    ('LN-CT1929877956-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929877956-4
    ('LN-CT1929900537-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929900537-4
    ('LN-CT1929955740-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929955740-4
    ('LN-CT1929987813-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929987813-4
    ('LN-CT1929999344-4', 'Customer Changed Their Mind', ''),  -- LN-CT-1929999344-4
    ('LN-EU01695382', 'Customer Changed Their Mind', ''),  -- LN-EU-01695382
    ('LN-IMP26110694363', 'Customer Changed Their Mind', ''),  -- LN-IM-P26110694363
    ('LN-IMP26110797936', 'Customer Changed Their Mind', ''),  -- LN-IM-P26110797936
    ('LN-IMP26110798128', 'Customer Changed Their Mind', ''),  -- LN-IM-P26110798128
    ('LN-IMP26110798129', 'Customer Changed Their Mind', ''),  -- LN-IM-P26110798129
    ('LN-IMP26110800106', 'Customer Changed Their Mind', ''),  -- LN-IM-P26110800106
    ('LN-IMP26110800616', 'Customer Changed Their Mind', ''),  -- LN-IM-P26110800616
    ('LN-IMP26110800617', 'Customer Changed Their Mind', ''),  -- LN-IM-P26110800617
    ('LN-IMP26110800618', 'Customer Changed Their Mind', ''),  -- LN-IM-P26110800618
    ('LN-IMP26110800619', 'Customer Changed Their Mind', ''),  -- LN-IM-P26110800619
    ('LN-IMP26110800620', 'Customer Changed Their Mind', ''),  -- LN-IM-P26110800620
    ('LN-IMP26110800621', 'Customer Changed Their Mind', ''),  -- LN-IM-P26110800621
    ('LN-IMP26110800622', 'Customer Changed Their Mind', ''),  -- LN-IM-P26110800622
    ('LN-IMP26110899231', 'Customer Changed Their Mind', ''),  -- LN-IM-P26110899231
    ('LN-IMP26110899294', 'Customer Changed Their Mind', ''),  -- LN-IM-P26110899294
    ('LN-IMP26110899315', 'Customer Changed Their Mind', ''),  -- LN-IM-P26110899315
    ('LN-IMP26110906541', 'Customer Changed Their Mind', ''),  -- LN-IM-P26110906541
    ('LN-IMP26111010092', 'Customer Changed Their Mind', ''),  -- LN-IM-P26111010092
    ('LN-IMP26111010888', 'Customer Changed Their Mind', ''),  -- LN-IM-P26111010888
    ('LN-IMP26111013377', 'Customer Changed Their Mind', ''),  -- LN-IM-P26111013377
    ('LN-IMP26111013453', 'Customer Changed Their Mind', ''),  -- LN-IM-P26111013453
    ('LN-IMP26111115134', 'Customer Changed Their Mind', ''),  -- LN-IM-P26111115134
    ('LN-IMP26111115709', 'Customer Changed Their Mind', ''),  -- LN-IM-P26111115709
    ('LN-IMP26111116473', 'Customer Changed Their Mind', ''),  -- LN-IM-P26111116473
    ('LN-IMP26111118872', 'Customer Changed Their Mind', ''),  -- LN-IM-P26111118872
    ('LN-IMP26111325800', 'Customer Changed Their Mind', ''),  -- LN-IM-P26111325800
    ('LN-IMP26111326418', 'Customer Changed Their Mind', ''),  -- LN-IM-P26111326418
    ('LN-IMP26111432708', 'Customer Changed Their Mind', ''),  -- LN-IM-P26111432708
    ('LN-IMP26111638486', 'Customer Changed Their Mind', ''),  -- LN-IM-P26111638486
    ('LN-NA00001641', 'Customer Changed Their Mind', ''),  -- LN-NA-00001641
    ('MA-EU00011828', 'Customer Changed Their Mind', ''),  -- MA-EUA00011828
    ('OW-EU00047483', 'Customer Changed Their Mind', ''),  -- OW-EU-00047483
    ('OW-EU00048044', 'Item Not Available', ''),  -- OW-EU-00048044
    ('OW-EU00049166', 'Item Not Available', ''),  -- OW-EU-00049166
    ('OW-EU00049848', 'No Inventory', ''),  -- OW-EU-00049848
    ('OW-FFOW7191109547', 'Customer Changed Their Mind', ''),  -- OW-FF-OW7191109547
    ('OW-FFOW7191153538', 'Customer Changed Their Mind', ''),  -- OW-FF-OW7191153538
    ('OW-US00038449', 'No Inventory', ''),  -- OW-US-00038449
    ('OW-US00039497', 'Customer Changed Their Mind', ''),  -- OW-US-00039497
    ('OW-US00039548', 'Other', 'Other'),  -- OW-US-00039548, OW-USA00039548
    ('PA-EU00019755', 'Need To Change Payment Method', ''),  -- PA-EU-00019755
    ('PJ-FFTGP191112973', 'Customer Changed Their Mind', ''),  -- PJ-FF-TGP191112973
    ('PJ-FFTGP191131034', 'Customer Changed Their Mind', ''),  -- PJ-FF-TGP191131034
    ('PJ-FFTGP191136162', 'Customer Changed Their Mind', ''),  -- PJ-FF-TGP191136162
    ('PL-MTBEFF54B2767184', 'Customer Changed Their Mind', ''),  -- PL-MT-BEFF54B2767184
    ('RC-EU00014332', 'Customer Changed Their Mind', ''),  -- RC-EUA00014332
    ('RC-US00014165', 'Customer Changed Their Mind', ''),  -- RC-US-00014165
    ('SC-EU01299882', 'No Inventory', ''),  -- SC-EU-01299882
    ('SC-EU01301277', 'No Inventory', ''),  -- SC-EU-01301277
    ('SC-FFTGA190998425', 'Customer Changed Their Mind', ''),  -- SC-FF-TGA190998425
    ('SC-FFTGA191015502', 'Customer Changed Their Mind', ''),  -- SC-FF-TGA191015502
    ('SC-MTNO8C655B128BC6', 'Item Not Available', ''),  -- SC-MT-NO8C655B128BC6
    ('SC-MTPL72FF8F315102', 'Other', 'Other'),  -- SC-MT-PL72ff8f315102
    ('SC-US00009255', 'Other', 'Other'),  -- SC-US-00009255
    ('SC-US00009368', 'Other', 'Other'),  -- SC-US-00009368
    ('SC-US00009377', 'Other', 'Other'),  -- SC-US-00009377
    ('SC-US00009597', 'Other', 'Other'),  -- SC-US-00009597
    ('SW-EU00828299', 'No Inventory', ''),  -- SW-EUG00828299
    ('SW-FFSW5191135828', 'Customer Changed Their Mind', ''),  -- SW-FF-SW5191135828
    ('SW-FFSW5191157718', 'Customer Changed Their Mind', ''),  -- SW-FF-SW5191157718
    ('WF-EU00011993', 'Customer Changed Their Mind', ''),  -- WF-EUB00011993
    ('WF-FFT3W191126708', 'Customer Changed Their Mind', ''),  -- WF-FF-T3W191126708
    ('WF-MK032805-3118708', 'No Inventory', ''),  -- WF-MK-032805-3118708
    ('WF-MTBE12D28C726265', 'Customer Changed Their Mind', ''),  -- WF-MT-BE12D28C726265
    ('WF-MTNO393664E8A721', 'Customer Changed Their Mind', ''),  -- WF-MT-NO393664E8A721
    ('WF-VP26024487', 'No Inventory', ''),  -- WF-VP-26024487
    ('WF-VP26075669', 'Other', 'Other'),  -- WF-VP-26075669
    ('WF-VP26152211', 'Other', 'Other'),  -- WF-VP-26152211
    ('WF-VP26180473', 'No Inventory', ''),  -- WF-VP-26180473
    ('WF-ZL10103739398086', 'Customer Changed Their Mind', ''),  -- WF-ZL-10103739398086
    ('WL-CT1926990038-4', 'Customer Changed Their Mind', ''),  -- WL-CT-1926990038-4
    ('WL-CT1929995878-4', 'Customer Changed Their Mind', ''),  -- WL-CT-1929995878-4
    ('WL-EU00590746', 'Customer Changed Their Mind', ''),  -- WL-EUA00590746
    ('WL-EU00592126', 'Other', 'Other'),  -- WL-EU-00592126
    ('WL-EU00592134', 'Other', 'Other'),  -- WL-EU-00592134
    ('WL-EU00592138', 'Other', 'Other'),  -- WL-EU-00592138
    ('WL-EU00592297', 'No Inventory', ''),  -- WL-EU-00592297
    ('WL-EU00592315', 'Item Not Available', ''),  -- WL-EU-00592315
    ('WL-EU00592338', 'Other', 'Other'),  -- WL-EU-00592338
    ('WL-FFWST191049400', 'Customer Changed Their Mind', '')  -- WL-FF-WST191049400
  ])
) AS s
WHERE t.shipment_order_number IS NOT NULL
  AND CONCAT(SUBSTR(UPPER(TRIM(t.shipment_order_number)), 1, 5),
             SUBSTR(UPPER(TRIM(t.shipment_order_number)), 7)) = s.match_key
  AND (t.reason_code IS NULL OR TRIM(t.reason_code) = '');


-- ---------------------------------------------------------------------------
-- 3. What it did
-- ---------------------------------------------------------------------------

SELECT
  IFNULL(NULLIF(TRIM(reason_code), ''), '(no reason)') AS reason_code,
  COUNT(*) AS shipments,
  COUNTIF(more_info IS NOT NULL AND TRIM(more_info) != '') AS with_more_info
FROM `tlg-business-intelligence-prd.til.warehouse_orders_cancellation_table`
GROUP BY 1
ORDER BY shipments DESC;
