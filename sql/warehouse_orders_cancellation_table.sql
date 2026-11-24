-- The warehouse cancellation ledger, as the two workers of this repository need it.
--
--   scripts/process_warehouse_cancellations.py  writes a row per shipment cancelled
--   scripts/reconcile_cancellations.py          confirms, resends and closes it in Kibo
--   the Cancellation ledger page of the applications repository reads it
--
-- Run this once, in the tlg-business-intelligence-prd project, before any of
-- them runs.
--
-- This is NOT the older til.orders_cancellation_table that the shipments_resolver
-- repository drives. That one stays where it is and is not touched here.

CREATE TABLE IF NOT EXISTS
  `tlg-business-intelligence-prd.til.warehouse_orders_cancellation_table`
(
  row_insertion_date TIMESTAMP
      OPTIONS (description = 'When the row was first inserted.'),
  row_last_modified_date TIMESTAMP
      OPTIONS (description = 'When any column of the row last changed.'),
  shipment_order_number STRING
      OPTIONS (description = 'The key: one row per shipment, e.g. OW-USA00037769.'),
  shipment_items STRING
      OPTIONS (description = "The shipment's product codes, joined by ';'."),
  cancellation_sent_to_warehouse BOOL
      OPTIONS (description = 'False when the CANCEL call to GXO failed, true when it went through.'),
  cancellation_sent_to_warehouse_date TIMESTAMP
      OPTIONS (description = 'When the CANCEL call was sent, or attempted.'),
  cancellation_confirmed_by_warehouse BOOL
      OPTIONS (description = 'True once prd_shipmentStatus carries a CANCELLED event with exactly these items.'),
  cancellation_confirmed_by_warehouse_date TIMESTAMP
      OPTIONS (description = 'The eventTimestamp of that CANCELLED event.'),
  number_of_cancellations_sent_to_warehouse INT64
      OPTIONS (description = '1 on the first successful send, raised by one on every resend.'),
  reason_code STRING
      OPTIONS (description = 'Why the shipment is being cancelled, as chosen in the console. One of the reasons Kibo accepts; sent as canceledReason.reasonCode when the shipment is cancelled in Kibo.'),
  more_info STRING
      OPTIONS (description = "Free text beside the reason, sent as canceledReason.moreInfo. Optional, and empty is fine — except on 'Other', where it carries the word 'Other' so the record still says something."),
  cancelled_on_kibo BOOL
      OPTIONS (description = 'True once the Kibo shipment is cancelled.'),
  cancelled_on_kibo_date TIMESTAMP
      OPTIONS (description = 'When the shipment was cancelled in Kibo.'),
  reassigned_on_kibo BOOL
      OPTIONS (description = 'True when Kibo moved this shipment to another location rather than cancelling it. The warehouse is still told to drop it; the reconciler leaves it alone, because there is no Kibo cancellation left to make.')
)
OPTIONS (
  description = 'Every shipment cancelled at the GXO warehouse by the operational_tools repository, and what became of it afterwards.'
);

-- If the table already exists from an earlier deployment, this adds whatever it
-- is missing and leaves every existing column alone. Safe to run next to the
-- CREATE above; both are no-ops once the table is complete.

ALTER TABLE `tlg-business-intelligence-prd.til.warehouse_orders_cancellation_table`
  ADD COLUMN IF NOT EXISTS row_insertion_date TIMESTAMP,
  ADD COLUMN IF NOT EXISTS row_last_modified_date TIMESTAMP,
  ADD COLUMN IF NOT EXISTS shipment_order_number STRING,
  ADD COLUMN IF NOT EXISTS shipment_items STRING,
  ADD COLUMN IF NOT EXISTS cancellation_sent_to_warehouse BOOL,
  ADD COLUMN IF NOT EXISTS cancellation_sent_to_warehouse_date TIMESTAMP,
  ADD COLUMN IF NOT EXISTS cancellation_confirmed_by_warehouse BOOL,
  ADD COLUMN IF NOT EXISTS cancellation_confirmed_by_warehouse_date TIMESTAMP,
  ADD COLUMN IF NOT EXISTS number_of_cancellations_sent_to_warehouse INT64,
  ADD COLUMN IF NOT EXISTS reason_code STRING,
  ADD COLUMN IF NOT EXISTS more_info STRING,
  ADD COLUMN IF NOT EXISTS reassigned_on_kibo BOOL
    OPTIONS (description = 'Kibo moved this shipment to another location rather than cancelling it. The warehouse is still told to drop it.'),
  ADD COLUMN IF NOT EXISTS cancelled_on_kibo BOOL,
  ADD COLUMN IF NOT EXISTS cancelled_on_kibo_date TIMESTAMP;

-- Optional. NULL already reads as false throughout both workers and the console,
-- so this is only for making the values explicit on rows written before a column
-- existed.
--
-- UPDATE `tlg-business-intelligence-prd.til.warehouse_orders_cancellation_table`
-- SET cancellation_sent_to_warehouse = IFNULL(cancellation_sent_to_warehouse, FALSE),
--     cancellation_confirmed_by_warehouse = IFNULL(cancellation_confirmed_by_warehouse, FALSE),
--     number_of_cancellations_sent_to_warehouse =
--         IFNULL(number_of_cancellations_sent_to_warehouse, 0),
--     cancelled_on_kibo = IFNULL(cancelled_on_kibo, FALSE)
-- WHERE TRUE;
