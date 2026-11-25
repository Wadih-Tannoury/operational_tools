-- The KO operational table
-- ========================
--
--   scripts/build_ko_monitoring.py   writes it on every run
--   the KO monitoring console        reads it
--
-- One row per shipment, for good: a shipment seen in a later upload has its
-- row updated rather than a second one added, and a shipment that stops
-- appearing in the uploads is marked ko_resolved rather than deleted.
--
-- This file creates the table as it is meant to be. An existing table from an
-- earlier version has different column names and a different order: run
-- sql/migrate_ko_operational_table.sql instead, once, and it rebuilds this
-- table from the old one without losing a row.
--
-- The service account behind BI_BIGQUERY_CREDS needs roles/bigquery.dataEditor
-- on this dataset, and read on til.prd_fulfillment_monitoring_table and
-- bi.wh_document_api_response. The console's own account needs read only.

CREATE TABLE IF NOT EXISTS
  `tlg-business-intelligence-prd.til.ko_operational_table`
(
  row_insertion_date TIMESTAMP
      OPTIONS (description = "When this shipment was first seen by a KO monitoring run."),
  row_last_modified_date TIMESTAMP
      OPTIONS (description = "When any column of the row last changed."),
  last_run STRING
      OPTIONS (description = "The stamp of the run that last touched this row."),
  shipment_order_number STRING
      OPTIONS (description = "The key: one row per shipment, e.g. OW-EUA00025115. VCVAIC on the workbook, Shipment on the CSV."),
  oerodp STRING
      OPTIONS (description = "OERODP from the workbook, or built from brandCode + the channel letter + the logistics order number when the workbook does not carry the shipment."),
  ub_imko STRING
      OPTIONS (description = "ub_imko from the workbook, normalised to 'IMKO 001'. Several values are joined by '; '."),
  logistic_order_number STRING
      OPTIONS (description = "The logistics order number. From the CSV, else from OERODP after its first hyphen, else read back from bi.wh_document_api_response."),
  oms_shipment_number STRING
      OPTIONS (description = "OMS shipment #, when the CSV export carries that column."),
  order_submit_date DATE
      OPTIONS (description = "Order date, from the CSV."),
  shipment_creation_date DATE
      OPTIONS (description = "Shipment created, from the CSV."),
  action STRING
      OPTIONS (description = "Action, from the CSV."),
  initial_fulfillment_status STRING
      OPTIONS (description = "What the shipment was doing when it first became a KO. Written once and never rewritten."),
  updated_fulfillment_status STRING
      OPTIONS (description = "What Kibo says the shipment is doing now. Refreshed on every run."),
  location_provider STRING
      OPTIONS (description = "Whose warehouse the shipment is in. GXO when the workbook carries it or the CSV's WMS says GXO, MODA when WMS says MODA, otherwise from til.prd_fulfillment_monitoring_table."),
  ko_fixed BOOL
      OPTIONS (description = "TRUE once ko_fix_date is set. Derived from it on every write, so the two can never disagree. A run only re-checks the shipments where this is FALSE."),
  ko_fix_date TIMESTAMP
      OPTIONS (description = "When the documents call first showed the KO fixed. Once set it is never overwritten."),
  last_tried_by_gxo_date TIMESTAMP
      OPTIONS (description = "When the warehouse last tried to print the documents, from bi.wh_document_api_response."),
  gxo_result_code STRING
      OPTIONS (description = "The result_code of that warehouse attempt."),
  tried_by_gxo_after_the_fix BOOL
      OPTIONS (description = "FALSE by default. TRUE once last_tried_by_gxo_date is later than ko_fix_date. Never goes back to FALSE."),
  ko_reason STRING
      OPTIONS (description = "Why the shipment is stuck. The first of these with anything to say: external_error_message, error_message or blocking_reason from til.prd_fulfillment_monitoring_table, then the documents API's error, then the CSV's action."),
  ko_resolved BOOL
      OPTIONS (description = "FALSE while the shipment keeps appearing in the uploads. TRUE once an upload no longer carries it, which is how a shipment leaves the dashboard without being deleted."),
  ko_resolved_date TIMESTAMP
      OPTIONS (description = "When ko_resolved was last set to TRUE."),
  source STRING
      OPTIONS (description = "Which side the row came from: 'TLG + GXO', 'TLG only' or 'GXO only'."),
  brand STRING
      OPTIONS (description = "The two-letter brand prefix."),
  eligible_for_package_closure BOOL
      OPTIONS (description = "eligibleForPackageClosure, as the documents API returned it."),
  label_shplbl_populated BOOL
      OPTIONS (description = "The SHPLBL entry carries label data."),
  label_shplbl_format STRING
      OPTIONS (description = "The shipping label's format: ZPL, PDF, PNG or other. ZPL and PDF are both printable by the warehouse."),
  label_retlbl_present BOOL
      OPTIONS (description = "A RETLBL entry exists in the response at all."),
  label_retlbl_populated BOOL
      OPTIONS (description = "The RETLBL entry carries label data."),
  label_retlbl_format STRING
      OPTIONS (description = "The return label's format: ZPL, PDF, PNG or other."),
  shipping_tracking_number STRING
      OPTIONS (description = "The tracking number on the SHPLBL entry."),
  return_tracking_number STRING
      OPTIONS (description = "The tracking number on the RETLBL entry."),
  doc_inv_populated BOOL
      OPTIONS (description = "The INV document carries a link."),
  doc_rpi_populated BOOL
      OPTIONS (description = "The RPI document carries a link."),
  api_call_date TIMESTAMP
      OPTIONS (description = "When the documents call was made."),
  batch STRING
      OPTIONS (description = "The upload batch the shipment last arrived in.")
)
OPTIONS (
  description = 'One row per shipment stuck without documents: what the KO monitoring workflow found, and whether it has since dropped out of the uploads.'
);


-- Adds whatever an existing table is missing, and leaves the rest alone.

ALTER TABLE `tlg-business-intelligence-prd.til.ko_operational_table`
  ADD COLUMN IF NOT EXISTS ko_source STRING
    OPTIONS (description = 'Who owns the KO, as til.prd_fulfillment_monitoring_table has it.'),
  ADD COLUMN IF NOT EXISTS blocking_reason STRING
    OPTIONS (description = 'Why the monitoring table says this shipment is blocked, as it has it.'),
  ADD COLUMN IF NOT EXISTS action_needed_from_gxo STRING
    OPTIONS (description = 'What GXO has to do: To cancel the shipment, To print shipping documents, or empty.'),
  ADD COLUMN IF NOT EXISTS channel STRING
    OPTIONS (description = 'The sales channel the order came through.'),
  ADD COLUMN IF NOT EXISTS in_latest_gxo_input BOOL
    OPTIONS (description = 'Was this shipment in the latest IMKO workbook or the latest monitoring.gxo_ko import? Set after every run that carried both.');

ALTER TABLE `tlg-business-intelligence-prd.til.ko_operational_table`
  ADD COLUMN IF NOT EXISTS row_insertion_date TIMESTAMP,
  ADD COLUMN IF NOT EXISTS row_last_modified_date TIMESTAMP,
  ADD COLUMN IF NOT EXISTS last_run STRING,
  ADD COLUMN IF NOT EXISTS shipment_order_number STRING,
  ADD COLUMN IF NOT EXISTS oerodp STRING,
  ADD COLUMN IF NOT EXISTS ub_imko STRING,
  ADD COLUMN IF NOT EXISTS logistic_order_number STRING,
  ADD COLUMN IF NOT EXISTS oms_shipment_number STRING,
  ADD COLUMN IF NOT EXISTS order_submit_date DATE,
  ADD COLUMN IF NOT EXISTS shipment_creation_date DATE,
  ADD COLUMN IF NOT EXISTS action STRING,
  ADD COLUMN IF NOT EXISTS initial_fulfillment_status STRING,
  ADD COLUMN IF NOT EXISTS updated_fulfillment_status STRING,
  ADD COLUMN IF NOT EXISTS location_provider STRING,
  ADD COLUMN IF NOT EXISTS ko_fixed BOOL,
  ADD COLUMN IF NOT EXISTS ko_fix_date TIMESTAMP,
  ADD COLUMN IF NOT EXISTS last_tried_by_gxo_date TIMESTAMP,
  ADD COLUMN IF NOT EXISTS gxo_result_code STRING,
  ADD COLUMN IF NOT EXISTS tried_by_gxo_after_the_fix BOOL,
  ADD COLUMN IF NOT EXISTS ko_reason STRING,
  ADD COLUMN IF NOT EXISTS ko_resolved BOOL,
  ADD COLUMN IF NOT EXISTS ko_resolved_date TIMESTAMP,
  ADD COLUMN IF NOT EXISTS source STRING,
  ADD COLUMN IF NOT EXISTS brand STRING,
  ADD COLUMN IF NOT EXISTS eligible_for_package_closure BOOL,
  ADD COLUMN IF NOT EXISTS label_shplbl_populated BOOL,
  ADD COLUMN IF NOT EXISTS label_shplbl_format STRING,
  ADD COLUMN IF NOT EXISTS label_retlbl_present BOOL,
  ADD COLUMN IF NOT EXISTS label_retlbl_populated BOOL,
  ADD COLUMN IF NOT EXISTS label_retlbl_format STRING,
  ADD COLUMN IF NOT EXISTS shipping_tracking_number STRING,
  ADD COLUMN IF NOT EXISTS return_tracking_number STRING,
  ADD COLUMN IF NOT EXISTS doc_inv_populated BOOL,
  ADD COLUMN IF NOT EXISTS doc_rpi_populated BOOL,
  ADD COLUMN IF NOT EXISTS api_call_date TIMESTAMP,
  ADD COLUMN IF NOT EXISTS batch STRING;
