#!/usr/bin/env python3
"""
Follow up on the cancellations already sent to the warehouse.

This worker runs on a schedule. It works from the ledger,
`tlg-business-intelligence-prd`.til.warehouse_orders_cancellation_table, which
scripts/process_warehouse_cancellations.py fills as it cancels, and it adds to
that ledger itself from the monitoring table (stage 0). It never touches
warehouse/input/ and shares no state with that worker beyond the table, so the
two run independently — an upload never triggers stage 0, and stage 0 never
waits for one.

Four stages, in this order.

0. The monitoring table
-----------------------
    SELECT * FROM `tlg-business-intelligence-prd`.til.prd_fulfillment_monitoring_table
    WHERE blocking_reason = 'Shipment cancelled on Kibo but still pending for GXO'

Kibo has settled these and the warehouse has not been told. Nobody uploads
them; they surface here. For each one the ledger does not already hold, Kibo's
getShipments says which way it was settled, the CANCEL event goes to the
warehouse, and a ledger row is written with:

    cancellation_sent_to_warehouse       = true
    cancellation_sent_to_warehouse_date  = now
    cancelled_on_kibo                    = true, when Kibo says CANCELED
    reassigned_on_kibo                   = true, when Kibo says REASSIGNED

That last pair is why stage 3 never picks these up: Kibo is already done with
them. Setting the sent date is what hands them to stage 1 and stage 2 — from
the next run they are ordinary rows, confirmed when the warehouse says so and
resent when it stays silent past --resend-after-hours.

A shipment Kibo shows as neither CANCELED nor REASSIGNED is skipped and
nothing is sent: the monitoring table and Kibo disagree, and the warehouse
event cannot be taken back.

1. Confirm
----------
Every row with cancellation_sent_to_warehouse = true and
cancellation_confirmed_by_warehouse = false is looked for in the warehouse's own
event feed:

    SELECT shipmentOrderNumber, eventType, eventTimestamp, requestPayload
    FROM `tlg-wlfs-prd`.til.prd_shipmentStatus
    WHERE shipmentOrderNumber IN UNNEST(@numbers)
      AND UPPER(eventType) IN ('CANCELLED', 'CANCELED')

A row is confirmed when one of those events carries exactly the items the ledger
holds in shipment_items — every item of the event is in the column and every item
of the column is in the event, not more and not less. Then:

    cancellation_confirmed_by_warehouse       = true
    cancellation_confirmed_by_warehouse_date  = that event's eventTimestamp

An event with other items on it confirms nothing: the warehouse cancelled
something, but not the shipment this row is about.

2. Resend
---------
A row still unconfirmed, whose cancellation went out at least --resend-after-hours
ago (24 by default), is sent again — the same CANCEL event the warehouse worker
sends, built from the same `tlg-wlfs-prd`.til.prd_orders GX rows:

    POST https://prdedi.gxo.com:4084/sync/mft/tlg_it/shipmentStatusUpdate

and on success:

    number_of_cancellations_sent_to_warehouse += 1
    cancellation_sent_to_warehouse_date        = now

Rows confirmed in stage 1 are out of scope by then, so a cancellation the
warehouse has acknowledged is never sent twice.

3. Cancel in Kibo
-----------------
Every row with cancellation_confirmed_by_warehouse = true and cancelled_on_kibo
= false is closed in Kibo. The shipment is found the long way round, because the
Kibo Shipment API is queried by order and not by shipmentOrderNumber:

    `tlg-wlfs-prd`.til.prd_orders    shipmentOrderNumber -> orderNumber
    GET /api/commerce/shipments?filter=externalOrderId=={orderNumber}

Of the shipments that come back, only the ones whose shipmentStatus is READY,
BACKORDER or CUSTOMER_CARE (--cancel-target-statuses) are cancelled, and by
default only those carrying exactly the items in shipment_items. The
shipmentOrderNumber does not identify one shipment — every generation of an order
can carry the same one, and where it does not, the live shipment carries a later
generation letter — so the items are what pick it out. A shipment whose items
Kibo has already moved on is read from reassignedItems instead, since the status
filter has by then ruled the reassigned ancestors out. --no-item-match drops the
gate and cancels every shipment of the order in one of those statuses;
--require-shipment-order-number tightens it the other way, to shipments carrying
the row's own number.

    PUT /api/commerce/shipments/{shipmentNumber}/canceled

and on success:

    cancelled_on_kibo       = true
    cancelled_on_kibo_date  = now

Credentials come from environment variables (GitHub repository secrets):
    BI_BIGQUERY_CREDS     service account JSON for tlg-business-intelligence-prd
    WLFS_BIGQUERY_CREDS   service account JSON for tlg-wlfs-prd
    KIBO_API_CREDS        {"client_id": "...", "client_secret": "..."}
    GXO_API_KEY           the endpoint key, raw or as {"api_key": "..."}

Outputs:
    warehouse/logs/reconcile-<stamp>.log    human readable, one line per row
    warehouse/logs/reconcile-<stamp>.csv    the same rows, structured
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

import cancellation_log  # noqa: E402
from cancellation_log import (  # noqa: E402
    CANCELLATION_TABLE, CancellationLedger, LedgerEntry, LedgerRow, Schema,
    bi_bigquery_client, clean, format_items,
    resolve_reason,
    load_schema, mark_cancelled_on_kibo_many, mark_confirmed_many, normalize_id,
    now_utc, parse_items, record_resend_many, rows_awaiting_confirmation,
    rows_confirmed_not_cancelled, same_items,
)

# The warehouse worker owns the GXO client, the Kibo client and the brand/tenant
# map. They are imported rather than copied so the two workers cannot drift apart
# on how a CANCEL event is built or how Kibo is talked to.
from process_warehouse_cancellations import (  # noqa: E402
    BRAND_TENANTS, DEFAULT_GXO_URL, DEFAULT_KIBO_AUTH_URL, DEFAULT_KIBO_HOST,
    DEFAULT_REASON_MESSAGE, DEFAULT_UPDATE_REASON, GxoClient, KiboClient, OrderRow,
    brand_prefix, gxo_api_key, is_terminal_status, kibo_credentials,
    load_tenant_map, shipment_number_of, shipment_order_number_of,
    shipment_status_of, shipments_in_payload,
)

try:
    from google.cloud import bigquery
    from google.oauth2 import service_account
except ImportError:
    bigquery = None  # type: ignore[assignment]
    service_account = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

WLFS_PROJECT = "tlg-wlfs-prd"
WLFS_DATASET = "til"
ORDERS_TABLE = "prd_orders"
SHIPMENT_STATUS_TABLE = "prd_shipmentStatus"

BQ_SCOPES = ["https://www.googleapis.com/auth/bigquery"]

# Both spellings are in the data.
CANCELLED_EVENT_TYPES = ("CANCELLED", "CANCELED")

# The statuses a shipment has to be in for Kibo to be worth cancelling. READY is
# the shipment the warehouse was working on; BACKORDER and CUSTOMER_CARE are
# where Kibo parks it once the warehouse has given it back.
DEFAULT_CANCEL_TARGET_STATUSES = "READY,BACKORDER,CUSTOMER_CARE"

DEFAULT_RESEND_AFTER_HOURS = 24.0

# ---------------------------------------------------------------------------
# Stage 0: the shipments the monitoring table reports
# ---------------------------------------------------------------------------
# Kibo has settled these — cancelled or reassigned — while the warehouse is
# still holding them. Nobody uploaded them; the monitoring table is where they
# surface. They belong to this workflow rather than to the upload one: they
# arrive on their own schedule, and once the CANCEL event is away they need
# exactly what this workflow already does for everything else — confirm it,
# and resend after --resend-after-hours if the warehouse stays silent.
MONITORING_TABLE = "tlg-business-intelligence-prd.til.prd_fulfillment_monitoring_table"
MONITORING_BLOCKING_REASON = "Shipment cancelled on Kibo but still pending for GXO"

# How Kibo's getShipments describes a shipment that is settled there.
KIBO_CANCELLED_STATUSES = {"CANCELED", "CANCELLED"}
KIBO_REASSIGNED_STATUSES = {"REASSIGNED"}

DEFAULT_KIBO_REASON_CODE = "CustomerChangedTheirMind"
DEFAULT_KIBO_MORE_INFO = "Cancelled by the warehouse"

# Product identifiers looked for anywhere inside a requestPayload.
PRODUCT_KEYS = (
    "upc", "sku", "productCode", "variationProductCode", "itemCode", "partNumber",
    "productId", "barcode", "ean", "styleCode",
)

# prd_shipmentStatus column names, resolved case-insensitively.
STATUS_NUMBER_CANDIDATES = ("shipmentOrderNumber", "shipment_order_number")
STATUS_EVENT_TYPE_CANDIDATES = ("eventType", "event_type")
STATUS_PAYLOAD_CANDIDATES = ("requestPayload", "request_payload")
STATUS_TIMESTAMP_CANDIDATES = (
    "eventTimestamp", "event_timestamp", "insertTimestamp", "insert_timestamp",
    "ingestionTimestamp", "ingestion_timestamp", "createdAt", "created_at",
)

# The Kibo site each brand's shipments live under. A wrong x-vol-site scopes a
# filtered query to a site holding none of the order's shipments and Kibo answers
# 200 with an empty page, which reads exactly like "this order has no shipments" —
# so the site matters as much as the tenant. Kept in step with BRAND_KIBO_CONFIG
# in the shipments_resolver repository.
BRAND_KIBO_SITES: Dict[str, str] = {
    "DG": "49791", "LN": "49801", "MO": "53100", "SC": "53620", "CO": "53190",
    "RC": "53192", "WL": "53613", "SW": "52246", "CA": "53602", "HB": "60955",
    "BA": "53088", "CV": "60958", "OW": "53145", "RL": "53145", "VW": "53605",
    "FA": "53605", "DJ": "53592", "AT": "53155", "LA": "53157", "CL": "53139",
    "PL": "53139", "HE": "53148", "FE": "53097", "CR": "53095", "EF": "53166",
    "FO": "53163", "PJ": "53179", "TM": "53493", "FU": "53493", "VE": "53636",
    "ZH": "53636", "WF": "53636", "BO": "54979", "MA": "55105", "FL": "55105",
    "PA": "55105", "AL": "55105", "BB": "54981",
}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def log(message: str) -> None:
    print(message, flush=True)


def warn(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def fail(message: str, exit_code: int = 1) -> None:
    print(f"ERROR: {message}", file=sys.stderr, flush=True)
    raise SystemExit(exit_code)


def normalize_status(value: Any) -> str:
    return clean(value).upper().replace(" ", "").replace("-", "_")


def wlfs_bigquery_client() -> Any:
    if bigquery is None:
        fail("google-cloud-bigquery is not installed; run pip install -r requirements.txt.")
    raw = os.environ.get("WLFS_BIGQUERY_CREDS", "").strip().lstrip("﻿")
    if not raw:
        fail("Environment variable WLFS_BIGQUERY_CREDS is empty. Check the repository secret.")
    try:
        info = json.loads(raw)
    except json.JSONDecodeError as exc:
        fail(f"WLFS_BIGQUERY_CREDS does not contain valid JSON: {exc}")
        return None
    credentials = service_account.Credentials.from_service_account_info(info, scopes=BQ_SCOPES)
    return bigquery.Client(project=clean(info.get("project_id")) or WLFS_PROJECT,
                           credentials=credentials)


def resolve_columns(client: Any, project: str, dataset: str,
                    table: str) -> Dict[str, str]:
    """{lower-cased column name: real column name} for one table."""
    sql = f"""
        SELECT column_name
        FROM `{project}.{dataset}`.INFORMATION_SCHEMA.COLUMNS
        WHERE table_name = @table
    """
    job = client.query(sql, job_config=bigquery.QueryJobConfig(query_parameters=[
        bigquery.ScalarQueryParameter("table", "STRING", table),
    ]))
    return {clean(row["column_name"]).lower(): clean(row["column_name"])
            for row in job.result()}


def pick_column(columns: Dict[str, str], candidates: Sequence[str],
                table: str, required: bool = True) -> str:
    for candidate in candidates:
        found = columns.get(candidate.lower())
        if found:
            return found
    if required:
        fail(f"{table} has none of the columns {list(candidates)}.")
    return ""


def products_in(value: Any, keys: Sequence[str] = PRODUCT_KEYS) -> List[str]:
    """
    Every product identifier anywhere inside a requestPayload.

    The payload arrives as a JSON string, as a STRUCT or as already-parsed JSON
    depending on how the column was loaded, so all three are walked and any value
    sitting under one of the product keys is kept.
    """
    found: List[str] = []

    def walk(node: Any) -> None:
        if node is None:
            return
        if isinstance(node, str):
            text = node.strip()
            if text[:1] in "[{":
                try:
                    walk(json.loads(text))
                except json.JSONDecodeError:
                    pass
            return
        if isinstance(node, dict):
            for key, child in node.items():
                if clean(key).lower() in {k.lower() for k in keys}:
                    text = clean(child)
                    if text:
                        found.append(text)
                else:
                    walk(child)
            return
        if isinstance(node, (list, tuple)):
            for child in node:
                walk(child)
            return
        # A BigQuery Row: a Sequence that knows its field names.
        if hasattr(node, "keys") and callable(getattr(node, "keys")):
            try:
                walk({key: node[key] for key in node.keys()})
            except Exception:  # noqa: BLE001  not a Row after all
                return

    walk(value)
    return sorted({clean(value) for value in found if clean(value)})


# ---------------------------------------------------------------------------
# Stage 0b — picked, then cancelled by the warehouse
# ---------------------------------------------------------------------------
# A shipment the warehouse has both PICKED and CANCELLED is a shipment whose
# goods were taken off the shelf and then given back. The cancellation reached
# GXO; what has not necessarily reached Kibo is that the shipment is over, so
# Kibo is told.
#
# Both events have to carry the same items, and every picked line has to be a
# single unit. A CANCELLED event that cancels part of what was picked is a
# partial, and cancelling the whole Kibo shipment for a partial would cancel
# lines the warehouse is still holding; a PICKED event for two or more of a line
# is the same risk from the other direction. The cartonId differs between the
# two events — it is the carton the goods were picked into, and a cancelled line
# goes back to stock — so it plays no part in the match.

PICKED_EVENT_TYPES = ("PICKED",)

# Kibo's shipmentNumber, out of the warehouse's logisticOrderNumber: the same
# number with the two-letter brand prefix taken off. OW109712 is Kibo shipment
# 109712. The pair (shipmentOrderNumber, shipmentNumber) is what identifies one
# shipment — the order number alone does not, because every generation of an
# order can carry it.
KIBO_SETTLED_STATUSES = KIBO_CANCELLED_STATUSES | KIBO_REASSIGNED_STATUSES


def kibo_shipment_number(logistic_order_number: Any) -> str:
    """Kibo's shipmentNumber for a logistics order number, or "" if there is none."""
    text = clean(logistic_order_number)
    return text[2:] if len(text) > 2 else ""


def event_items(payload: Any) -> Tuple[Tuple[str, int], ...]:
    """
    The items of one shipmentStatus event, as (productId, quantity) pairs.

    Sorted and hashable so two events can be compared directly. The quantity
    counts: picking two and cancelling one is not the same shipment's worth of
    goods, and the pair catches that where a bare set of product codes would
    not.

    The payload arrives as JSON text, as a STRUCT or as parsed JSON depending
    on how the column was loaded, so it is normalised the same way products_in
    normalises it.
    """
    node = payload
    if isinstance(node, str):
        text = node.strip()
        if text[:1] not in "[{":
            return ()
        try:
            node = json.loads(text)
        except json.JSONDecodeError:
            return ()
    if hasattr(node, "keys") and callable(getattr(node, "keys")) \
            and not isinstance(node, dict):
        try:
            node = {key: node[key] for key in node.keys()}
        except Exception:  # noqa: BLE001 - not a Row after all
            return ()
    if not isinstance(node, dict):
        return ()

    products = node.get("products")
    if not isinstance(products, list):
        return ()

    found: List[Tuple[str, int]] = []
    for item in products:
        if not isinstance(item, dict):
            continue
        code = ""
        for key in ("productId", "productCode", "sku", "upc", "barcode"):
            code = clean(item.get(key))
            if code:
                break
        if not code:
            continue
        try:
            quantity = int(item.get("quantity") or 1)
        except (TypeError, ValueError):
            quantity = 1
        found.append((code, quantity))
    return tuple(sorted(found))


@dataclass
class PickedCancellation:
    shipment_order_number: str = ""
    logistic_order_number: str = ""
    items: Tuple[Tuple[str, int], ...] = ()
    cancelled_event_id: str = ""
    cancelled_at: Optional[datetime] = None
    picked_at: Optional[datetime] = None

    @property
    def item_text(self) -> str:
        return ", ".join(f"{code}x{quantity}" for code, quantity in self.items)


def fetch_picked_then_cancelled(client: Any, limit: int = 0,
                                lookback_days: int = 0
                                ) -> Tuple[List[PickedCancellation], str]:
    """
    Every shipment with a CANCELLED event and a PICKED event for the same items,
    where every picked line is a single unit.

    Matched on shipmentOrderNumber and logisticOrderNumber together: the
    shipment number alone does not identify one shipment, and the logistics
    order is what GXO is working to. Both come out of the payload, where the
    logistics order number is the only place it is recorded.

    The item comparison is done here rather than in SQL. The payload is a JSON
    blob whose shape varies by loader, and BigQuery would need the products
    array flattened three different ways to cope with that; event_items already
    normalises it, and the volume is a few thousand events.
    """
    table = f"{WLFS_PROJECT}.{WLFS_DATASET}.{SHIPMENT_STATUS_TABLE}"
    try:
        columns = resolve_columns(client, WLFS_PROJECT, WLFS_DATASET,
                                  SHIPMENT_STATUS_TABLE)
    except Exception as exc:  # noqa: BLE001
        return [], f"could not read the schema of {table}: {exc}"

    event_col = pick_column(columns, STATUS_EVENT_TYPE_CANDIDATES, table)
    payload_col = pick_column(columns, STATUS_PAYLOAD_CANDIDATES, table)
    number_col = pick_column(columns, STATUS_NUMBER_CANDIDATES, table, required=False)
    timestamp_col = pick_column(columns, STATUS_TIMESTAMP_CANDIDATES, table,
                                required=False)
    id_col = pick_column(columns, ("eventId", "event_id"), table, required=False)

    timestamp_select = (f"CAST(`{timestamp_col}` AS TIMESTAMP)" if timestamp_col
                        else "CAST(NULL AS TIMESTAMP)")
    number_select = (f"CAST(`{number_col}` AS STRING)" if number_col
                     else "CAST(NULL AS STRING)")
    id_select = f"CAST(`{id_col}` AS STRING)" if id_col else "CAST(NULL AS STRING)"
    window = ""
    if lookback_days and lookback_days > 0 and timestamp_col:
        window = (f"AND `{timestamp_col}` >= "
                  f"TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {int(lookback_days)} DAY)")

    sql = f"""
        SELECT
            UPPER(TRIM(CAST(`{event_col}` AS STRING))) AS event_type,
            {number_select} AS shipment_order_number,
            {id_select} AS event_id,
            `{payload_col}` AS request_payload,
            {timestamp_select} AS event_timestamp
        FROM `{table}`
        WHERE UPPER(TRIM(CAST(`{event_col}` AS STRING))) IN UNNEST(@types)
        {window}
    """
    parameters = [bigquery.ArrayQueryParameter(
        "types", "STRING",
        [t.upper() for t in (*CANCELLED_EVENT_TYPES, *PICKED_EVENT_TYPES)],
    )]
    try:
        rows = list(client.query(
            sql, job_config=bigquery.QueryJobConfig(query_parameters=parameters)
        ).result())
    except Exception as exc:  # noqa: BLE001
        return [], f"could not read {table}: {exc}"

    cancelled: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    picked: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for row in rows:
        payload = row["request_payload"]
        number = clean(row["shipment_order_number"]) or payload_field(
            payload, "shipmentOrderNumber")
        logistic = payload_field(payload, "logisticOrderNumber")
        if not number:
            continue
        key = (normalize_id(number), normalize_id(logistic))
        entry = {
            "number": number,
            "logistic": logistic,
            "items": event_items(payload),
            "event_id": clean(row["event_id"]),
            "at": row["event_timestamp"],
        }
        bucket = (cancelled if clean(row["event_type"]).upper()
                  in {t.upper() for t in CANCELLED_EVENT_TYPES} else picked)
        bucket.setdefault(key, []).append(entry)

    found: List[PickedCancellation] = []
    multi_unit = 0
    for key, cancel_events in cancelled.items():
        pick_events = picked.get(key)
        if not pick_events:
            continue
        for cancel_event in cancel_events:
            if not cancel_event["items"]:
                continue
            match = next((pick for pick in pick_events
                          if pick["items"] == cancel_event["items"]), None)
            if match is None:
                continue
            # Every picked line has to be a single unit. A PICKED event for two
            # or more of something is not one shipment's worth of goods coming
            # back off the shelf, and cancelling the whole Kibo shipment on the
            # strength of it would close lines the warehouse may still hold.
            # Those are left for a person to look at rather than guessed.
            if any(quantity != 1 for _, quantity in match["items"]):
                multi_unit += 1
                continue
            found.append(PickedCancellation(
                shipment_order_number=cancel_event["number"],
                logistic_order_number=cancel_event["logistic"],
                items=cancel_event["items"],
                cancelled_event_id=cancel_event["event_id"],
                cancelled_at=cancel_event["at"],
                picked_at=match["at"],
            ))
            break

    if multi_unit:
        log(f"[picked] {multi_unit} shipment(s) match on items but the PICKED "
            "event carries more than one unit of a line; they are left alone.")

    found.sort(key=lambda item: item.shipment_order_number)
    if limit and limit > 0:
        found = found[:limit]
    return found, ""


def payload_field(payload: Any, name: str) -> str:
    """One named field from anywhere inside a shipmentStatus payload."""
    values = products_in(payload, keys=(name,))
    return values[0] if values else ""


# The OMS view of what is still live. A shipment that is not in here with a
# status other than CANCELED has nothing left to cancel: either the OMS has
# already cancelled it, or it is not an active shipment at all. Checked before
# Kibo, because it is one query for the whole candidate list where Kibo is one
# call per shipment.
ACTIVE_SHIPMENTS_TABLE = "oms_active_shipments"
ACTIVE_NUMBER_CANDIDATES = ("shipmentOrderNumber", "shipment_order_number",
                            "externalOrderId", "external_order_id")
ACTIVE_STATUS_CANDIDATES = ("shipmentStatus", "shipment_status", "status")


def fetch_active_shipments(bi_client: Any) -> Tuple[Optional[Set[str]], str]:
    """
    The shipments the OMS still shows as something other than CANCELED.

    Returns None rather than an empty set when the table cannot be read, and
    the caller then narrows nothing: an unreadable table must not look like an
    OMS with no active shipments, which would silently turn the whole stage
    off.
    """
    table = (f"{cancellation_log.BI_PROJECT}.{cancellation_log.BI_DATASET}."
             f"{ACTIVE_SHIPMENTS_TABLE}")
    try:
        columns = resolve_columns(bi_client, cancellation_log.BI_PROJECT,
                                  cancellation_log.BI_DATASET,
                                  ACTIVE_SHIPMENTS_TABLE)
    except Exception as exc:  # noqa: BLE001
        return None, f"could not read the schema of {table}: {exc}"
    if not columns:
        return None, f"{table} has no columns to read"

    try:
        number_col = pick_column(columns, ACTIVE_NUMBER_CANDIDATES, table)
        status_col = pick_column(columns, ACTIVE_STATUS_CANDIDATES, table)
    except Exception as exc:  # noqa: BLE001
        return None, str(exc)

    sql = f"""
        SELECT DISTINCT
          UPPER(TRIM(CAST(`{number_col}` AS STRING))) AS shipment_order_number
        FROM `{table}`
        WHERE UPPER(TRIM(IFNULL(CAST(`{status_col}` AS STRING), ''))) NOT IN UNNEST(@cancelled)
          AND `{number_col}` IS NOT NULL
    """
    parameters = [bigquery.ArrayQueryParameter(
        "cancelled", "STRING", sorted({s.upper() for s in KIBO_CANCELLED_STATUSES}))]
    try:
        rows = list(bi_client.query(
            sql, job_config=bigquery.QueryJobConfig(query_parameters=parameters)
        ).result())
    except Exception as exc:  # noqa: BLE001
        return None, f"could not read {table}: {exc}"
    return {normalize_id(row["shipment_order_number"]) for row in rows}, ""


def run_picked_cancellations(bi_client: Any, wlfs_client: Any,
                             kibo: Optional[KiboClient],
                             tenant_map: Dict[str, str],
                             args: argparse.Namespace) -> List[Outcome]:
    """
    Cancel in Kibo every shipment the warehouse picked and then cancelled.

    The warehouse has already finished with these: it picked the goods, then
    cancelled, and the stock is back. Nothing is sent to GXO here — the
    cancellation is theirs and it is done. What this closes is the other half,
    the Kibo shipment that is still sitting in READY or parked in BACKORDER or
    CUSTOMER_CARE while nobody is working on it. Kibo is asked first: a
    shipment it has already cancelled or reassigned is reported as such and
    left alone.
    """
    outcomes: List[Outcome] = []
    if wlfs_client is None:
        return [Outcome(stage="picked", outcome="failed",
                        detail="no BigQuery client for the warehouse project")]

    found, error = fetch_picked_then_cancelled(
        wlfs_client, args.limit, args.picked_lookback_days)
    if error:
        return [Outcome(stage="picked", outcome="failed", detail=error)]
    log(f"[picked] {len(found)} shipment(s) have a CANCELLED event and a PICKED "
        "event carrying the same items.")
    if not found:
        return outcomes

    # Narrowed against the OMS before a single Kibo call. A shipment the OMS
    # does not hold as something other than CANCELED has nothing left to
    # cancel, and the warehouse's events go back far enough that most of this
    # list is history. One query here saves one Kibo call per shipment, and
    # more to the point it keeps the stage from acting on shipments that were
    # settled long ago.
    active, active_error = fetch_active_shipments(bi_client)
    if active_error:
        log(f"[picked] {ACTIVE_SHIPMENTS_TABLE} could not be read "
            f"({active_error}); the candidates were not narrowed.")
    elif active is not None:
        before = len(found)
        found = [item for item in found
                 if normalize_id(item.shipment_order_number) in active]
        log(f"[picked] {before - len(found)} of {before} are not active in "
            f"{ACTIVE_SHIPMENTS_TABLE} with a status other than CANCELED; "
            f"{len(found)} left to look at.")
        if not found:
            return outcomes

    if kibo is None:
        return [Outcome(stage="picked", outcome="failed",
                        detail="no Kibo client; nothing was cancelled")]

    wanted_statuses = [clean(status) for status
                       in args.cancel_target_statuses.split(",") if clean(status)]

    for item in found:
        base = dict(stage="picked", shipment_order_number=item.shipment_order_number,
                    shipment_items=item.item_text, event_id=item.cancelled_event_id)
        tenant, site, tenant_error = tenant_and_site(
            item.shipment_order_number, tenant_map, args.default_tenant_id,
            args.default_site_id)
        if tenant_error:
            outcomes.append(Outcome(**base, outcome="skipped", detail=tenant_error))
            continue

        shipments, _, lookup_error = kibo.get_shipments_for_order(
            tenant, site, item.shipment_order_number)
        if lookup_error:
            outcomes.append(Outcome(**base, tenant_id=tenant, outcome="failed",
                                    detail=f"the Kibo lookup failed: {lookup_error}"))
            continue

        # One shipment, not the whole order. The pair that identifies it is the
        # shipmentOrderNumber, which found the order, and the shipmentNumber,
        # which is the logistics order number without its brand prefix. Without
        # this the stage would act on every generation of the order, and the
        # generation GXO picked and cancelled is only one of them.
        wanted_number = kibo_shipment_number(item.logistic_order_number)
        if not wanted_number:
            outcomes.append(Outcome(
                **base, tenant_id=tenant, outcome="skipped",
                detail="the event carries no logisticOrderNumber, so the Kibo "
                       "shipment cannot be identified"))
            continue
        mine = [shipment for shipment in shipments
                if clean(shipment.get("shipmentNumber")) == wanted_number]
        if not mine:
            numbers = ", ".join(sorted({clean(s.get("shipmentNumber"))
                                        for s in shipments})) or "none at all"
            outcomes.append(Outcome(
                **base, tenant_id=tenant, outcome="skipped",
                detail=f"Kibo has no shipment {wanted_number} on this order "
                       f"(it has {numbers})"))
            continue

        # Cancelled or reassigned, Kibo has settled it and there is nothing to
        # send. Said in those words rather than left to the status filter
        # below, which would report it as "nothing to cancel" — and that reads
        # like something went wrong, when in fact both halves agree.
        settled = [shipment for shipment in mine
                   if normalize_status(shipment_status_of(shipment))
                   in {normalize_status(status) for status in KIBO_SETTLED_STATUSES}]
        if settled:
            how = ", ".join(sorted({clean(shipment_status_of(shipment)).upper()
                                    for shipment in settled}))
            outcomes.append(Outcome(
                **base, tenant_id=tenant, outcome="skipped",
                kibo_shipment_number=wanted_number,
                detail=f"Kibo already has shipment {wanted_number} as {how}; "
                       "nothing was sent"))
            continue

        targets = [shipment for shipment in mine
                   if normalize_status(shipment_status_of(shipment)) in
                   {normalize_status(status) for status in wanted_statuses}]
        if not targets:
            statuses = ", ".join(sorted({clean(shipment_status_of(s)) or "(none)"
                                         for s in mine})) or "none at all"
            outcomes.append(Outcome(
                **base, tenant_id=tenant, kibo_shipment_number=wanted_number,
                outcome="skipped",
                detail=f"Kibo shipment {wanted_number} is {statuses}, which is "
                       "not a status worth cancelling"))
            continue

        if args.dry_run:
            outcomes.append(Outcome(
                **base, tenant_id=tenant, outcome="dry_run",
                kibo_shipment_number=", ".join(
                    clean(s.get("shipmentNumber")) for s in targets),
                detail="would be cancelled in Kibo"))
            continue

        done, problems = [], []
        for shipment in targets:
            number = clean(shipment.get("shipmentNumber"))
            # The tenant and site the shipment itself came back with, exactly
            # as stage 1 does it. The order lookup is deliberately tenant-wide
            # because a brand can have several sites, so the shipment in hand
            # is the only thing that knows which one it is on, and a PUT to
            # the wrong site is a cancellation that does not land: Kibo
            # answers 404 for a shipment the search had just returned.
            shipment_tenant = clean(str(shipment.get("tenantId") or "")) or tenant
            shipment_site = clean(str(shipment.get("siteId") or "")) or site
            ok, send_error, excerpt = kibo.cancel_shipment(
                shipment_tenant, shipment_site, number, args.kibo_reason_code,
                f"Picked and cancelled at the warehouse "
                f"(event {item.cancelled_event_id})"[:200],
                args.kibo_send_notifications,
            )
            if ok:
                done.append(f"{number}[{clean(shipment_status_of(shipment))}]")
            else:
                problems.append(f"{number}: {send_error} {excerpt}"[:200])

        outcomes.append(Outcome(
            # The numbers that were tried, not only the ones that worked: a
            # failed row with an empty column is a row that cannot be looked
            # up from the log it was reported in.
            **base, tenant_id=tenant,
            kibo_shipment_number=", ".join(done) or ", ".join(
                clean(shipment.get("shipmentNumber")) for shipment in targets),
            outcome="failed" if problems and not done else
                    ("cancelled" if done else "skipped"),
            detail="; ".join(problems) if problems else
                   f"picked {item.picked_at.isoformat() if item.picked_at else 'unknown date'}, "
                   f"cancelled {item.cancelled_at.isoformat() if item.cancelled_at else 'unknown date'}"))

    cancelled_count = sum(1 for o in outcomes if o.outcome == "cancelled")
    already = sum(1 for o in outcomes
                  if o.outcome == "skipped" and "already has shipment" in o.detail)
    log(f"[picked] {cancelled_count} shipment(s) cancelled in Kibo"
        + (f", {already} already cancelled or reassigned there" if already else "")
        + ".")
    return outcomes


# ---------------------------------------------------------------------------
# Stage 1 — the warehouse's own CANCELLED events
# ---------------------------------------------------------------------------

@dataclass
class CancelEvent:
    shipment_order_number: str = ""
    products: List[str] = field(default_factory=list)
    timestamp: Optional[datetime] = None


def fetch_cancelled_events(client: Any, numbers: Sequence[str]
                           ) -> Tuple[Dict[str, List[CancelEvent]], str]:
    """
    The CANCELLED events of the given shipmentOrderNumbers, newest first.

    Keyed on the normalised shipmentOrderNumber, which is how the ledger's rows
    are looked up against them.
    """
    if not numbers:
        return {}, ""
    table = f"{WLFS_PROJECT}.{WLFS_DATASET}.{SHIPMENT_STATUS_TABLE}"
    try:
        columns = resolve_columns(client, WLFS_PROJECT, WLFS_DATASET,
                                  SHIPMENT_STATUS_TABLE)
    except Exception as exc:  # noqa: BLE001
        return {}, f"could not read the schema of {table}: {exc}"

    number_col = pick_column(columns, STATUS_NUMBER_CANDIDATES, table)
    event_col = pick_column(columns, STATUS_EVENT_TYPE_CANDIDATES, table)
    payload_col = pick_column(columns, STATUS_PAYLOAD_CANDIDATES, table)
    timestamp_col = pick_column(columns, STATUS_TIMESTAMP_CANDIDATES, table, required=False)

    timestamp_select = (f"CAST(`{timestamp_col}` AS TIMESTAMP)" if timestamp_col
                        else "CAST(NULL AS TIMESTAMP)")
    order_by = f"ORDER BY event_timestamp DESC" if timestamp_col else ""
    sql = f"""
        SELECT
            CAST(`{number_col}` AS STRING) AS shipment_order_number,
            `{payload_col}` AS request_payload,
            {timestamp_select} AS event_timestamp
        FROM `{table}`
        WHERE UPPER(TRIM(CAST(`{number_col}` AS STRING))) IN UNNEST(@numbers)
          AND UPPER(TRIM(CAST(`{event_col}` AS STRING))) IN UNNEST(@types)
        {order_by}
    """
    parameters = [
        bigquery.ArrayQueryParameter(
            "numbers", "STRING", sorted({clean(n).upper() for n in numbers if clean(n)})
        ),
        bigquery.ArrayQueryParameter("types", "STRING", list(CANCELLED_EVENT_TYPES)),
    ]
    try:
        rows = list(client.query(
            sql, job_config=bigquery.QueryJobConfig(query_parameters=parameters)
        ).result())
    except Exception as exc:  # noqa: BLE001
        return {}, f"could not read {table}: {exc}"

    events: Dict[str, List[CancelEvent]] = {}
    for row in rows:
        number = clean(row["shipment_order_number"])
        if not number:
            continue
        events.setdefault(normalize_id(number), []).append(CancelEvent(
            shipment_order_number=number,
            products=products_in(row["request_payload"]),
            timestamp=row["event_timestamp"],
        ))
    return events, ""


def confirming_event(row: LedgerRow,
                     events: Sequence[CancelEvent]) -> Optional[CancelEvent]:
    """
    The CANCELLED event that confirms this row, if there is one.

    'The same items, not more and not less': an event listing a superset or a
    subset of shipment_items is about a different shipment of the same order and
    confirms nothing. Events arrive newest first, so the first match is the most
    recent one.
    """
    if not row.items:
        return None
    for event in events:
        if same_items(event.products, row.items):
            return event
    return None


# ---------------------------------------------------------------------------
# prd_orders — the shipment's order and its GX location rows
# ---------------------------------------------------------------------------

@dataclass
class OrderFacts:
    """What prd_orders knows about one shipmentOrderNumber."""
    shipment_order_number: str = ""
    order_number: str = ""
    gx_rows: List[OrderRow] = field(default_factory=list)


def fetch_order_facts(client: Any, numbers: Sequence[str]
                      ) -> Tuple[Dict[str, OrderFacts], str]:
    """
    One read of prd_orders for the whole run: the orderNumber of each shipment,
    and its GX location rows — the first is what Kibo is queried by, the second
    what a resent CANCEL event is built from.
    """
    if not numbers:
        return {}, ""
    sql = f"""
        SELECT
            o.shipmentOrderNumber AS shipmentOrderNumber,
            o.orderNumber         AS orderNumber,
            o.logisticOrderNumber AS logisticOrderNumber,
            o.locationName        AS locationName,
            o.brand               AS brand,
            bc.brandCode          AS brandCode
        FROM `{WLFS_PROJECT}.{WLFS_DATASET}.{ORDERS_TABLE}` AS o
        LEFT JOIN `{WLFS_PROJECT}.{WLFS_DATASET}.prd_brandConfig` AS bc
            ON o.brand = bc.brand
        WHERE UPPER(TRIM(o.shipmentOrderNumber)) IN UNNEST(@numbers)
    """
    parameters = [bigquery.ArrayQueryParameter(
        "numbers", "STRING", sorted({clean(n).upper() for n in numbers if clean(n)})
    )]
    try:
        rows = list(client.query(
            sql, job_config=bigquery.QueryJobConfig(query_parameters=parameters)
        ).result())
    except Exception as exc:  # noqa: BLE001
        return {}, f"could not read {WLFS_PROJECT}.{WLFS_DATASET}.{ORDERS_TABLE}: {exc}"

    facts: Dict[str, OrderFacts] = {}
    for row in rows:
        number = clean(row.get("shipmentOrderNumber"))
        if not number:
            continue
        entry = facts.setdefault(normalize_id(number),
                                 OrderFacts(shipment_order_number=number))
        if not entry.order_number:
            entry.order_number = clean(row.get("orderNumber"))
        location = clean(row.get("locationName"))
        # Only a GX location can be cancelled at the warehouse, which is the one
        # thing the GX rows are used for.
        if "GX" in location.upper():
            entry.gx_rows.append(OrderRow(
                shipment_order_number=number,
                logistic_order_number=clean(row.get("logisticOrderNumber")),
                location_name=location,
                brand=clean(row.get("brand")),
                brand_code=clean(row.get("brandCode")),
            ))
    return facts, ""


# ---------------------------------------------------------------------------
# Kibo addressing
# ---------------------------------------------------------------------------

def tenant_and_site(shipment_order_number: str, tenant_map: Dict[str, str],
                    default_tenant: str, default_site: str) -> Tuple[str, str, str]:
    """(tenantId, siteId, error) for a shipmentOrderNumber's brand prefix."""
    prefix = brand_prefix(shipment_order_number)
    tenant = tenant_map.get(prefix, "") or default_tenant
    site = BRAND_KIBO_SITES.get(prefix, "") or default_site
    if not tenant:
        return "", "", (f"no Kibo tenant for brand prefix '{prefix or '?'}': add it to "
                        "KIBO_TENANT_MAP or set --default-tenant-id")
    return tenant, site, ""


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------

@dataclass
class Outcome:
    stage: str = ""                 # monitoring | picked | confirm | resend | kibo
    shipment_order_number: str = ""
    shipment_items: str = ""
    order_number: str = ""
    tenant_id: str = ""
    kibo_shipment_number: str = ""
    event_id: str = ""
    outcome: str = ""               # confirmed | sent | cancelled | skipped | failed | dry_run
    detail: str = ""

    @property
    def failed(self) -> bool:
        return self.outcome == "failed"


OUTCOME_FIELDS = [
    "stage", "shipment_order_number", "shipment_items", "order_number", "tenant_id",
    "kibo_shipment_number", "event_id", "outcome", "detail",
]


# ---------------------------------------------------------------------------
# Stage 1
# ---------------------------------------------------------------------------

def fetch_monitoring_shipments(bi_client: Any, limit: int = 0) -> Tuple[List[str], str]:
    """
    Shipments the monitoring table reports as cancelled on Kibo and still
    pending for GXO.

    Returns the shipment order numbers and an error; an error costs the run
    this stage, not the run.
    """
    sql = f"""
        SELECT DISTINCT
          NULLIF(TRIM(CAST(shipment_order_number AS STRING)), '') AS shipment_order_number
        FROM `{MONITORING_TABLE}`
        WHERE TRIM(CAST(blocking_reason AS STRING)) = @reason
          AND NULLIF(TRIM(CAST(shipment_order_number AS STRING)), '') IS NOT NULL
        ORDER BY shipment_order_number
        {f'LIMIT {int(limit)}' if limit and limit > 0 else ''}
    """
    try:
        job = bi_client.query(sql, job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("reason", "STRING", MONITORING_BLOCKING_REASON),
        ]))
        return [clean(row["shipment_order_number"]) for row in job.result()], ""
    except Exception as exc:  # noqa: BLE001 - the other stages still run
        return [], f"{MONITORING_TABLE} could not be read: {exc}"


def already_in_ledger(bi_client: Any, schema: Schema,
                      numbers: Sequence[str]) -> Tuple[set, str]:
    """Which of these shipments the ledger already holds, whatever their state."""
    wanted = sorted({normalize_id(number) for number in numbers if clean(number)})
    if not wanted:
        return set(), ""
    column = schema.column("shipment_order_number")
    # normalize_id is blind to spaces and hyphens, so the column has to be put
    # through the same mill before it is compared — UPPER(TRIM(...)) alone
    # would never match a hyphenated shipment number.
    normalised = (f"UPPER(REGEXP_REPLACE(CAST(`{column}` AS STRING), "
                  r"r'[\s_\-]+', ''))")
    sql = f"""
        SELECT DISTINCT {normalised} AS shipment
        FROM `{cancellation_log.BI_PROJECT}.{cancellation_log.BI_DATASET}.{CANCELLATION_TABLE}`
        WHERE {normalised} IN UNNEST(@wanted)
    """
    try:
        job = bi_client.query(sql, job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ArrayQueryParameter("wanted", "STRING", wanted)]))
        return {clean(row["shipment"]) for row in job.result()}, ""
    except Exception as exc:  # noqa: BLE001
        return set(), f"the ledger could not be read: {exc}"


def kibo_settlement(kibo: Optional[KiboClient], number: str,
                    tenant_map: Dict[str, str], args: argparse.Namespace
                    ) -> Tuple[str, List[Dict[str, Any]], str]:
    """
    How Kibo has settled this shipment: (CANCELED | REASSIGNED | "", shipments, error).

    A cancellation outranks a reassignment when the number answers for both:
    the shipment the warehouse is holding is the one that was cancelled.
    """
    if kibo is None:
        return "", [], "no Kibo client was built; check KIBO_API_CREDS"
    tenant_id, site_id, tenant_error = tenant_and_site(
        number, tenant_map, args.default_tenant_id, args.default_site_id)
    if tenant_error:
        return "", [], tenant_error
    # Every spelling of the number, not just the derived one. The sixth
    # character is a site letter in some brands and part of the order number in
    # others — DG-EUF0188365364 is its own externalOrderId, while
    # OW-USA00037769 is OW-US-00037769 — so both are tried, raw first. This is
    # the same helper the other stages use, which is the point: one rule.
    shipments, spelling, error = kibo.get_shipments_for_order(
        tenant_id, site_id, number)
    if error:
        return "", [], f"getShipments for '{number}' failed: {error}"
    if not shipments:
        return "", [], (f"Kibo returned no shipment for {number} "
                        f"(tried {spelling or 'nothing'})")
    matching = [
        shipment for shipment in shipments
        if normalize_id(shipment_order_number_of(shipment)) == normalize_id(number)
    ]
    statuses = {shipment_status_of(shipment).strip().upper() for shipment in matching}
    if statuses & KIBO_CANCELLED_STATUSES:
        return "CANCELED", matching, ""
    if statuses & KIBO_REASSIGNED_STATUSES:
        return "REASSIGNED", matching, ""
    return "", matching, ""


def run_monitoring(bi_client: Any, schema: Schema, wlfs_client: Any,
                   kibo: Optional[KiboClient], gxo: Optional[GxoClient],
                   tenant_map: Dict[str, str],
                   args: argparse.Namespace) -> List[Outcome]:
    """
    Take the monitoring table's shipments into the ledger and tell the
    warehouse to drop them.

    Only shipments the ledger does not already hold: one that is there is
    already this workflow's business, and the resend stage will chase the
    warehouse for it on its own schedule. A shipment taken in here is written
    with cancellation_sent_to_warehouse set, so the very next run treats it
    like any other — confirmed when the warehouse says so, resent when it does
    not within --resend-after-hours.
    """
    outcomes: List[Outcome] = []
    numbers, error = fetch_monitoring_shipments(bi_client, args.limit)
    if error:
        log(f"[monitoring] {error}")
        return [Outcome(stage="monitoring", outcome="failed", detail=error)]
    if not numbers:
        log(f"[monitoring] no shipment with '{MONITORING_BLOCKING_REASON}'.")
        return outcomes

    known, read_error = already_in_ledger(bi_client, schema, numbers)
    if read_error:
        log(f"[monitoring] {read_error}")
        return [Outcome(stage="monitoring", outcome="failed", detail=read_error)]
    fresh = [number for number in numbers if normalize_id(number) not in known]
    log(f"[monitoring] {len(numbers)} shipment(s) reported, "
        f"{len(numbers) - len(fresh)} already in the ledger, {len(fresh)} to take in.")
    if not fresh:
        return outcomes

    facts, facts_error = fetch_order_facts(wlfs_client, fresh)
    if facts_error:
        log(f"[monitoring] {facts_error}")

    ledger = CancellationLedger(client=bi_client, schema=schema, dry_run=args.dry_run)
    for number in fresh:
        base = dict(stage="monitoring", shipment_order_number=number)
        settled, shipments, kibo_error = kibo_settlement(kibo, number, tenant_map, args)
        if kibo_error:
            # Without Kibo there is no way to say which way it was settled, and
            # a row that cannot say is worse than no row: the cancellation is
            # left for the next run rather than sent blind.
            outcomes.append(Outcome(**base, outcome="failed", detail=kibo_error))
            continue
        if not settled:
            statuses = sorted({shipment_status_of(shipment).strip().upper()
                               for shipment in shipments}) or ["nothing"]
            outcomes.append(Outcome(
                **base, outcome="skipped",
                detail="Kibo shows this shipment as "
                       + ", ".join(statuses)
                       + ", not CANCELED or REASSIGNED, so the monitoring table and "
                         "Kibo disagree; nothing was sent"))
            continue

        entry = facts.get(normalize_id(number))
        gx_rows = entry.gx_rows if entry else []
        if not gx_rows:
            outcomes.append(Outcome(
                **base, outcome="failed",
                order_number=entry.order_number if entry else "",
                detail=f"{number} has no GX location row in "
                       f"{WLFS_PROJECT}.{WLFS_DATASET}.{ORDERS_TABLE}, so no CANCEL "
                       "event could be built"))
            continue

        sent_any = False
        details: List[str] = []
        event_ids: List[str] = []
        for order in gx_rows:
            payload = gxo.build_payload(order, args.reason_message) if gxo else {}
            event_ids.append(clean(payload.get("eventId")))
            if args.dry_run or gxo is None:
                details.append(f"would POST {json.dumps(payload.get('data', {}))[:200]}")
                continue
            status_code, send_error, excerpt = gxo.send(payload)
            if send_error:
                details.append(f"{order.location_name or '?'}: {send_error} {excerpt}"[:300])
            else:
                sent_any = True
                details.append(f"{order.location_name or '?'}: HTTP {status_code}")

        detail = f"kibo says {settled}; " + "; ".join(details)
        if args.dry_run:
            outcomes.append(Outcome(**base, outcome="dry_run",
                                    order_number=entry.order_number if entry else "",
                                    event_id=", ".join(filter(None, event_ids)),
                                    detail=detail[:500]))
            continue
        if not sent_any:
            outcomes.append(Outcome(**base, outcome="failed",
                                    order_number=entry.order_number if entry else "",
                                    event_id=", ".join(filter(None, event_ids)),
                                    detail="the warehouse refused every event: "
                                           + detail[:400]))
            continue

        ok, message = ledger.record(LedgerEntry(
            shipment_order_number=number,
            items=format_items(cancellation_log.items_of_shipments(shipments)),
            sent_to_warehouse=True,
            sent_at=now_utc(),
            # Kibo is already done with this shipment either way, so the row
            # says so from the start and the Kibo stage never picks it up.
            cancelled_on_kibo=settled == "CANCELED",
            cancelled_on_kibo_at=now_utc() if settled == "CANCELED" else None,
            reassigned_on_kibo=settled == "REASSIGNED",
        ))
        outcomes.append(Outcome(
            **base, outcome="sent",
            order_number=entry.order_number if entry else "",
            event_id=", ".join(filter(None, event_ids)),
            detail=detail[:400] + ("; the ledger was written" if ok
                                   else f"; the ledger was not written: {message}")))
    return outcomes


def run_confirm(bi_client: Any, schema: Schema, wlfs_client: Any,
                args: argparse.Namespace) -> Tuple[List[Outcome], List[LedgerRow]]:
    """
    Confirm what the warehouse has acknowledged.

    Returns (outcomes, the rows that are STILL unconfirmed) — the second is what
    stage 2 works from, so a row confirmed here is never resent.
    """
    outcomes: List[Outcome] = []
    rows, error = rows_awaiting_confirmation(bi_client, schema)
    if error:
        outcomes.append(Outcome(stage="confirm", outcome="failed", detail=error))
        return outcomes, []
    log(f"[confirm] {len(rows)} row(s) sent to the warehouse and not confirmed yet.")
    if not rows:
        return outcomes, []

    events, error = fetch_cancelled_events(
        wlfs_client, [row.shipment_order_number for row in rows]
    )
    if error:
        outcomes.append(Outcome(stage="confirm", outcome="failed", detail=error))
        return outcomes, rows

    still_open: List[LedgerRow] = []
    confirmations: List[Tuple[LedgerRow, Any, Dict[str, Any]]] = []
    for row in rows:
        found = events.get(normalize_id(row.shipment_order_number), [])
        base = dict(stage="confirm", shipment_order_number=row.shipment_order_number,
                    shipment_items=row.raw_items)
        if not row.items:
            still_open.append(row)
            outcomes.append(Outcome(
                **base, outcome="skipped",
                detail="the row has no shipment_items, so no CANCELLED event can be "
                       "matched to it; nothing was confirmed",
            ))
            continue
        event = confirming_event(row, found)
        if event is None:
            still_open.append(row)
            outcomes.append(Outcome(
                **base, outcome="skipped",
                detail=(f"{len(found)} CANCELLED event(s) in {SHIPMENT_STATUS_TABLE}, "
                        "none carrying exactly these items"
                        if found else
                        f"no CANCELLED event in {SHIPMENT_STATUS_TABLE}"),
            ))
            continue
        if args.dry_run:
            outcomes.append(Outcome(
                **base, outcome="dry_run",
                detail=f"would confirm from the CANCELLED event of "
                       f"{event.timestamp.isoformat() if event.timestamp else 'unknown date'}",
            ))
            continue
        # Collected, not written one by one: the whole stage goes out in a
        # single UPDATE below. A statement per shipment is a few seconds each,
        # which is ten minutes of waiting on a busy day.
        confirmations.append((row, event, dict(base)))

    # One statement for the whole stage. All of them are written or none are:
    # a failure leaves every one of these rows unconfirmed, and the next run
    # tries them again, which is the same place a per-row failure left them.
    if confirmations:
        _, update_error = mark_confirmed_many(
            bi_client, schema,
            [(row.shipment_order_number, event.timestamp) for row, event, _ in confirmations],
        )
        for row, event, base in confirmations:
            when = event.timestamp.isoformat() if event.timestamp else "unknown date"
            if update_error:
                still_open.append(row)
                outcomes.append(Outcome(
                    **base, outcome="failed",
                    detail=f"could not write the confirmation: {update_error}"))
            else:
                outcomes.append(Outcome(
                    **base, outcome="confirmed",
                    detail=f"confirmed from the CANCELLED event of {when}"))
        if not update_error:
            log(f"[confirm] {len(confirmations)} row(s) confirmed in one statement.")

    return outcomes, still_open


# ---------------------------------------------------------------------------
# Stage 2
# ---------------------------------------------------------------------------

def run_resend(bi_client: Any, schema: Schema, rows: Sequence[LedgerRow],
               facts: Dict[str, OrderFacts], gxo: Optional[GxoClient],
               args: argparse.Namespace) -> List[Outcome]:
    """Send the CANCEL event again for everything the warehouse never acknowledged."""
    outcomes: List[Outcome] = []
    resent: List[Tuple[LedgerRow, Outcome]] = []
    cutoff = now_utc() - timedelta(hours=args.resend_after_hours)
    due = [
        row for row in rows
        if row.sent_to_warehouse and row.sent_at is not None and row.sent_at <= cutoff
    ]
    waiting = len(rows) - len(due)
    log(f"[resend] {len(due)} row(s) due for a resend "
        f"(sent before {cutoff.isoformat(timespec='seconds')}); "
        f"{waiting} still inside the {args.resend_after_hours}h window or never sent.")

    for row in due:
        base = dict(stage="resend", shipment_order_number=row.shipment_order_number,
                    shipment_items=row.raw_items)
        entry = facts.get(normalize_id(row.shipment_order_number))
        gx_rows = entry.gx_rows if entry else []
        if not gx_rows:
            outcomes.append(Outcome(
                **base, outcome="failed",
                order_number=entry.order_number if entry else "",
                detail=f"{row.shipment_order_number} has no GX location row in "
                       f"{WLFS_PROJECT}.{WLFS_DATASET}.{ORDERS_TABLE}, so no CANCEL event "
                       "could be built",
            ))
            continue

        sent_any = False
        details: List[str] = []
        event_ids: List[str] = []
        for order in gx_rows:
            payload = gxo.build_payload(order, args.reason_message) if gxo else {}
            event_ids.append(clean(payload.get("eventId")))
            if args.dry_run or gxo is None:
                details.append(f"would POST {json.dumps(payload.get('data', {}))[:200]}")
                continue
            status_code, error, excerpt = gxo.send(payload)
            if error:
                details.append(f"{order.location_name or '?'}: {error} {excerpt}"[:300])
            else:
                sent_any = True
                details.append(f"{order.location_name or '?'}: HTTP {status_code}")

        if args.dry_run:
            outcomes.append(Outcome(**base, outcome="dry_run",
                                    order_number=entry.order_number if entry else "",
                                    event_id=", ".join(filter(None, event_ids)),
                                    detail="; ".join(details)[:500]))
            continue
        if not sent_any:
            outcomes.append(Outcome(**base, outcome="failed",
                                    order_number=entry.order_number if entry else "",
                                    event_id=", ".join(filter(None, event_ids)),
                                    detail="the warehouse refused every event: "
                                           + "; ".join(details)[:400]))
            continue

        # The event is away; the ledger catches up for the whole stage below.
        resent.append((row, Outcome(
            **base, outcome="sent",
            order_number=entry.order_number if entry else "",
            event_id=", ".join(filter(None, event_ids)),
            detail=f"resent, attempt {row.sends + 1}; " + "; ".join(details)[:300],
        )))

    if resent:
        _, update_error = record_resend_many(
            bi_client, schema, [row.shipment_order_number for row, _ in resent])
        for _, outcome in resent:
            outcome.detail += (f"; the ledger was not updated: {update_error}"
                               if update_error else "; the ledger was updated")
            outcomes.append(outcome)
    return outcomes


# ---------------------------------------------------------------------------
# Stage 3
# ---------------------------------------------------------------------------

def choose_kibo_targets(row: LedgerRow, shipments: Sequence[Dict[str, Any]],
                        wanted_statuses: Sequence[str],
                        args: argparse.Namespace) -> Tuple[List[Dict[str, Any]], str]:
    """
    The shipments of the order that this row should cancel, and a note saying how
    they were chosen.

    Status first: only READY, BACKORDER or CUSTOMER_CARE is worth a cancellation.
    Then the items, because the shipmentOrderNumber does not identify one
    shipment: every generation of an order can carry the same one, and where it
    does not, the live shipment carries a later generation letter. Either way the
    number cannot pick the shipment out and the items can.
    """
    wanted = {normalize_status(status) for status in wanted_statuses}
    candidates = [
        shipment for shipment in shipments
        if normalize_status(shipment_status_of(shipment)) in wanted
        and not is_terminal_status(shipment_status_of(shipment))
    ]
    if not candidates:
        return [], (f"none of the {len(shipments)} shipment(s) of the order is "
                    f"{', '.join(sorted(wanted))}")

    if args.require_shipment_order_number:
        narrowed = [
            shipment for shipment in candidates
            if normalize_id(shipment_order_number_of(shipment))
            == normalize_id(row.shipment_order_number)
        ]
        if not narrowed:
            return [], (f"{len(candidates)} shipment(s) in a cancellable status, none "
                        f"carrying shipmentOrderNumber {row.shipment_order_number}")
        return narrowed, f"{len(narrowed)} matched by shipmentOrderNumber"

    if not args.item_match:
        return candidates, f"{len(candidates)} in a cancellable status (item gate off)"

    if not row.items:
        return [], ("the row has no shipment_items, so no shipment of the order can be "
                    "identified; cancel it by hand or run with --no-item-match")

    # include_moved: Kibo empties `items` when it hands the goods on, and a
    # shipment can be read back in a cancellable status with its products still
    # in reassignedItems. The statuses have already been filtered, so a
    # REASSIGNED ancestor cannot be picked up this way.
    narrowed = [
        shipment for shipment in candidates
        if same_items(cancellation_log.shipment_item_codes(shipment, include_moved=True),
                      row.items)
    ]
    if not narrowed:
        seen = "; ".join(
            f"{shipment_number_of(shipment) or '?'}"
            f"[{shipment_status_of(shipment) or '?'}]="
            + format_items(cancellation_log.shipment_item_codes(
                shipment, include_moved=True))
            for shipment in candidates
        )
        return [], (f"{len(candidates)} shipment(s) in a cancellable status, none "
                    f"carrying exactly {row.raw_items} — {seen}"[:400])
    return narrowed, f"{len(narrowed)} matched by items"


def run_kibo(bi_client: Any, schema: Schema, kibo: Optional[KiboClient],
             facts: Dict[str, OrderFacts], tenant_map: Dict[str, str],
             args: argparse.Namespace) -> List[Outcome]:
    outcomes: List[Outcome] = []
    done: List[Tuple[LedgerRow, Outcome]] = []
    rows, error = rows_confirmed_not_cancelled(bi_client, schema)
    if error:
        outcomes.append(Outcome(stage="kibo", outcome="failed", detail=error))
        return outcomes
    log(f"[kibo] {len(rows)} row(s) confirmed by the warehouse and still open in Kibo.")

    wanted_statuses = [
        clean(status) for status in args.cancel_target_statuses.split(",") if clean(status)
    ]

    for row in rows:
        base = dict(stage="kibo", shipment_order_number=row.shipment_order_number,
                    shipment_items=row.raw_items)
        entry = facts.get(normalize_id(row.shipment_order_number))
        order_number = entry.order_number if entry else ""
        if not order_number:
            outcomes.append(Outcome(
                **base, outcome="failed",
                detail=f"{row.shipment_order_number} has no orderNumber in "
                       f"{WLFS_PROJECT}.{WLFS_DATASET}.{ORDERS_TABLE}, so Kibo cannot be "
                       "queried by externalOrderId",
            ))
            continue

        tenant_id, site_id, tenant_error = tenant_and_site(
            row.shipment_order_number, tenant_map, args.default_tenant_id,
            args.default_site_id,
        )
        if tenant_error:
            outcomes.append(Outcome(**base, outcome="failed", order_number=order_number,
                                    detail=tenant_error))
            continue

        if kibo is None:
            outcomes.append(Outcome(**base, outcome="failed", order_number=order_number,
                                    tenant_id=tenant_id,
                                    detail="no Kibo client was built; check KIBO_API_CREDS"))
            continue

        kibo.forget_shipments()
        # Both spellings of the number, because Kibo's externalOrderId carries a
        # hyphen where prd_orders and the warehouse carry a generation letter.
        shipments, used, read_error = kibo.get_shipments_for_order(
            tenant_id, site_id, order_number, row.shipment_order_number
        )
        if read_error:
            outcomes.append(Outcome(
                **base, outcome="failed", order_number=order_number, tenant_id=tenant_id,
                detail=f"getShipments by externalOrderId '{used or order_number}' "
                       f"failed: {read_error}",
            ))
            continue
        if not shipments:
            tried = ", ".join(
                f"'{candidate}'" for candidate in
                kibo.order_id_variants(order_number, row.shipment_order_number)
            )
            outcomes.append(Outcome(
                **base, outcome="skipped", order_number=order_number, tenant_id=tenant_id,
                detail=f"no shipment in Kibo on tenant {tenant_id} for externalOrderId "
                       f"{tried}; the order may belong to another tenant",
            ))
            continue

        targets, note = choose_kibo_targets(row, shipments, wanted_statuses, args)
        if not targets:
            outcomes.append(Outcome(**base, outcome="skipped", order_number=order_number,
                                    tenant_id=tenant_id, detail=note))
            continue

        cancelled: List[str] = []
        problems: List[str] = []
        for shipment in targets:
            shipment_number = shipment_number_of(shipment)
            status = shipment_status_of(shipment)
            if not shipment_number:
                problems.append("a shipment came back without a shipmentNumber")
                continue
            if args.dry_run:
                cancelled.append(f"{shipment_number}[{status}]")
                continue
            # The reason the person chose when they queued the cancellation,
            # read back off the ledger row. The run-wide defaults are only for
            # rows recorded before the console asked for a reason.
            reason_code, more_info = resolve_reason(
                row.reason_code or args.kibo_reason_code,
                row.more_info or args.kibo_more_info,
            )
            # The shipment's own site, not the brand's mapped one: it was
            # found tenant-wide precisely because those can differ, and a PUT
            # to the wrong site is a cancellation that does not land.
            shipment_site = clean(str(shipment.get("siteId") or "")) or site_id
            shipment_tenant = clean(str(shipment.get("tenantId") or "")) or tenant_id
            ok, send_error, excerpt = kibo.cancel_shipment(
                shipment_tenant, shipment_site, shipment_number, reason_code,
                more_info, args.kibo_send_notifications,
            )
            if ok:
                cancelled.append(f"{shipment_number}[{status}]")
            else:
                problems.append(f"{shipment_number}: {send_error} {excerpt}"[:200])

        shared = dict(**base, order_number=order_number, tenant_id=tenant_id,
                      kibo_shipment_number=", ".join(cancelled))
        if args.dry_run:
            outcomes.append(Outcome(**shared, outcome="dry_run",
                                    detail=f"{note}; would cancel {', '.join(cancelled)}"))
            continue
        if not cancelled:
            outcomes.append(Outcome(**shared, outcome="failed",
                                    detail=f"{note}; nothing was cancelled: "
                                           + "; ".join(problems)[:400]))
            continue

        done.append((row, Outcome(
            **shared, outcome="cancelled",
            detail=(f"{note}; cancelled {', '.join(cancelled)}"
                    + (f"; also tried {'; '.join(problems)}"[:200] if problems else "")),
        )))

    if done:
        _, update_error = mark_cancelled_on_kibo_many(
            bi_client, schema, [row.shipment_order_number for row, _ in done])
        for _, outcome in done:
            outcome.detail += (f"; the ledger was not updated: {update_error}"
                               if update_error else "; the ledger was updated")
            outcomes.append(outcome)
    return outcomes


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def write_outputs(log_dir: Path, stamp: str, outcomes: Sequence[Outcome],
                  args: argparse.Namespace, started_at: datetime) -> Tuple[Path, Path]:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"reconcile-{stamp}.log"
    csv_path = log_dir / f"reconcile-{stamp}.csv"

    counts: Dict[str, int] = {}
    for outcome in outcomes:
        counts[outcome.outcome] = counts.get(outcome.outcome, 0) + 1

    lines = [
        f"Run started   : {started_at.isoformat(timespec='seconds')}",
        f"Run finished  : {now_utc().isoformat(timespec='seconds')}",
        f"Mode          : {'DRY RUN - nothing was sent or written' if args.dry_run else 'LIVE'}",
        f"Ledger        : {cancellation_log.BI_PROJECT}.{cancellation_log.BI_DATASET}."
        f"{CANCELLATION_TABLE}",
        f"Resend after  : {args.resend_after_hours}h without a CANCELLED event",
        f"Kibo targets  : {args.cancel_target_statuses}"
        + ("" if args.item_match else " (item gate off)"),
        "",
        "Counts: " + (", ".join(f"{name}={count}" for name, count in sorted(counts.items()))
                      or "nothing to do"),
        "",
    ]
    for outcome in outcomes:
        lines.append(
            f"[{outcome.stage}] {outcome.outcome.upper():<9} "
            f"{outcome.shipment_order_number or '-'} "
            f"items={outcome.shipment_items or '-'} "
            f"order={outcome.order_number or '-'}"
            + (f" shipment={outcome.kibo_shipment_number}"
               if outcome.kibo_shipment_number else "")
            + (f" :: {outcome.detail}" if outcome.detail else "")
        )
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with csv_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=OUTCOME_FIELDS)
        writer.writeheader()
        for outcome in outcomes:
            data = asdict(outcome)
            writer.writerow({key: data.get(key, "") for key in OUTCOME_FIELDS})
    return log_path, csv_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Confirm, resend and close the cancellations in the ledger.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--log-dir", type=Path, default=Path("warehouse/logs"))
    parser.add_argument("--resend-after-hours", type=float,
                        default=DEFAULT_RESEND_AFTER_HOURS,
                        help="How long a cancellation may go unconfirmed before it is "
                             "sent to the warehouse again.")
    parser.add_argument("--cancel-target-statuses", default=DEFAULT_CANCEL_TARGET_STATUSES,
                        help="Comma separated Kibo shipmentStatus values a shipment must "
                             "be in to be cancelled.")
    parser.add_argument("--no-item-match", dest="item_match", action="store_false",
                        help="Cancel every shipment of the order in a target status, "
                             "rather than only the ones carrying exactly the row's "
                             "shipment_items.")
    parser.set_defaults(item_match=True)
    parser.add_argument("--require-shipment-order-number", action="store_true",
                        help="Only cancel a shipment carrying the row's own "
                             "shipmentOrderNumber. Rarely narrows anything — every "
                             "generation of an order can carry the same number — and "
                             "misses the reassigned generation the goods end up in when "
                             "it does not, so it is off by default.")
    parser.add_argument("--skip-monitoring", action="store_true",
                        help="do not take shipments from "
                             "til.prd_fulfillment_monitoring_table")
    parser.add_argument("--skip-picked", action="store_true",
                        help="do not cancel in Kibo the shipments the warehouse "
                             "picked and then cancelled")
    parser.add_argument("--picked-lookback-days", type=int, default=30,
                        help="how far back to read PICKED and CANCELLED events "
                             "for that stage (0 = the whole table). The default "
                             "keeps the scan to a month: a shipment picked and "
                             "cancelled long ago has been settled by some other "
                             "route by now.")
    parser.add_argument("--skip-confirm", action="store_true",
                        help="Do not look for CANCELLED events.")
    parser.add_argument("--skip-resend", action="store_true",
                        help="Do not resend anything to the warehouse.")
    parser.add_argument("--skip-kibo", action="store_true",
                        help="Do not cancel anything in Kibo.")
    parser.add_argument("--gxo-url", default=os.environ.get("GXO_URL") or DEFAULT_GXO_URL)
    parser.add_argument("--update-reason", default=DEFAULT_UPDATE_REASON)
    parser.add_argument("--reason-message", default=DEFAULT_REASON_MESSAGE)
    parser.add_argument("--host", default=os.environ.get("KIBO_HOST") or DEFAULT_KIBO_HOST)
    parser.add_argument("--auth-url", default=DEFAULT_KIBO_AUTH_URL)
    parser.add_argument("--tenant-map", default="")
    parser.add_argument("--default-tenant-id",
                        default=os.environ.get("KIBO_TENANT_ID") or "")
    parser.add_argument("--default-site-id", default=os.environ.get("KIBO_SITE_ID") or "")
    parser.add_argument("--kibo-reason-code", default=DEFAULT_KIBO_REASON_CODE)
    parser.add_argument("--kibo-more-info", default=DEFAULT_KIBO_MORE_INFO)
    parser.add_argument("--no-kibo-send-notifications", dest="kibo_send_notifications",
                        action="store_false")
    parser.set_defaults(kibo_send_notifications=True)
    parser.add_argument("--dry-run", action="store_true",
                        help="Resolve and report, but send nothing and write nothing.")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--max-retries", type=int, default=3)
    parser.add_argument("--limit", type=int, default=0,
                        help="Maximum rows per stage, 0 for no limit.")

    args = parser.parse_args()
    if args.resend_after_hours < 0:
        parser.error("--resend-after-hours cannot be negative.")
    if args.max_retries < 1:
        parser.error("--max-retries must be at least 1.")
    args.host = re.sub(r"^https?://", "", args.host.strip()).split("/", 1)[0]
    args.host = re.sub(r"^t\d+(?:-s\d+)?\.", "", args.host, flags=re.IGNORECASE).rstrip(".")
    if not args.host:
        parser.error("--host cannot be empty.")
    return args


def main() -> int:
    args = parse_args()
    started_at = now_utc()
    stamp = started_at.strftime("%Y%m%d-%H%M%S")
    if args.dry_run:
        log("DRY RUN: nothing will be sent to the warehouse or to Kibo, and the ledger "
            "will not be written.")

    bi_client, error = bi_bigquery_client()
    if error:
        fail(f"the ledger is unreachable: {error}")
    schema = load_schema(bi_client)
    if schema.error:
        fail(schema.error)
    if schema.missing:
        warn(f"{CANCELLATION_TABLE} has no {', '.join(schema.missing)} column; "
             "run sql/warehouse_orders_cancellation_table.sql. The stages that need them do "
             "nothing until it is there.")

    wlfs_client = wlfs_bigquery_client()
    outcomes: List[Outcome] = []
    tenant_map = load_tenant_map(args.tenant_map)

    # 0. The shipments the monitoring table reports: Kibo has settled them and
    #    the warehouse has not been told. They come in here rather than through
    #    the upload queue, and from the next run on they are ordinary ledger
    #    rows — confirmed when the warehouse says so, resent when it does not.
    if args.skip_monitoring:
        log("[monitoring] skipped (--skip-monitoring).")
    else:
        monitoring_gxo = GxoClient(
            url=args.gxo_url,
            api_key="dry-run" if args.dry_run else gxo_api_key(),
            timeout=args.timeout, max_retries=args.max_retries,
            update_reason=args.update_reason)
        monitoring_kibo = KiboClient(
            credentials=kibo_credentials(), auth_url=args.auth_url,
            host=args.host, timeout=args.timeout, max_retries=args.max_retries)
        outcomes.extend(run_monitoring(bi_client, schema, wlfs_client, monitoring_kibo,
                                       monitoring_gxo, tenant_map, args))

    # 0b. Picked, then cancelled by the warehouse. Nothing goes to GXO here:
    #     the warehouse picked the goods, cancelled, and put them back. What is
    #     left open is the Kibo side, so that is what this closes.
    if args.skip_picked:
        log("[picked] skipped (--skip-picked).")
    else:
        picked_kibo = KiboClient(
            credentials=kibo_credentials(), auth_url=args.auth_url,
            host=args.host, timeout=args.timeout, max_retries=args.max_retries)
        outcomes.extend(run_picked_cancellations(
            bi_client, wlfs_client, picked_kibo, tenant_map, args))

    # 1. Confirm.
    unconfirmed: List[LedgerRow] = []
    if args.skip_confirm:
        log("[confirm] skipped (--skip-confirm).")
        unconfirmed, read_error = rows_awaiting_confirmation(bi_client, schema)
        if read_error:
            outcomes.append(Outcome(stage="confirm", outcome="failed", detail=read_error))
    else:
        confirm_outcomes, unconfirmed = run_confirm(bi_client, schema, wlfs_client, args)
        outcomes.extend(confirm_outcomes)

    # 2 and 3 share one read of prd_orders.
    to_resend = unconfirmed if not args.skip_resend else []
    kibo_rows: List[LedgerRow] = []
    if not args.skip_kibo:
        kibo_rows, read_error = rows_confirmed_not_cancelled(bi_client, schema)
        if read_error:
            outcomes.append(Outcome(stage="kibo", outcome="failed", detail=read_error))
    if args.limit > 0:
        to_resend = to_resend[: args.limit]
        kibo_rows = kibo_rows[: args.limit]

    facts: Dict[str, OrderFacts] = {}
    numbers = sorted({
        row.shipment_order_number for row in list(to_resend) + list(kibo_rows)
    })
    if numbers:
        facts, facts_error = fetch_order_facts(wlfs_client, numbers)
        if facts_error:
            outcomes.append(Outcome(stage="resend", outcome="failed", detail=facts_error))

    # 2. Resend.
    if args.skip_resend:
        log("[resend] skipped (--skip-resend).")
    elif to_resend:
        # A dry run still builds the client, so the log can show the payload that
        # would go out; it just never calls send().
        gxo = GxoClient(url=args.gxo_url,
                        api_key="dry-run" if args.dry_run else gxo_api_key(),
                        timeout=args.timeout, max_retries=args.max_retries,
                        update_reason=args.update_reason)
        outcomes.extend(run_resend(bi_client, schema, to_resend, facts, gxo, args))
    else:
        log("[resend] nothing is due.")

    # 3. Cancel in Kibo.
    if args.skip_kibo:
        log("[kibo] skipped (--skip-kibo).")
    elif kibo_rows:
        kibo = KiboClient(credentials=kibo_credentials(), auth_url=args.auth_url,
                          host=args.host, timeout=args.timeout,
                          max_retries=args.max_retries)
        outcomes.extend(run_kibo(bi_client, schema, kibo, facts, tenant_map, args))
    else:
        log("[kibo] nothing is confirmed and still open.")

    for outcome in outcomes:
        log(f"[{outcome.stage}] {outcome.outcome.upper()} "
            f"{outcome.shipment_order_number or '-'}"
            + (f" :: {outcome.detail}" if outcome.detail else ""))

    log_path, csv_path = write_outputs(args.log_dir, stamp, outcomes, args, started_at)
    confirmed = sum(1 for o in outcomes if o.outcome == "confirmed")
    resent = sum(1 for o in outcomes if o.outcome == "sent" and o.stage == "resend")
    taken = sum(1 for o in outcomes if o.outcome == "sent" and o.stage == "monitoring")
    cancelled = sum(1 for o in outcomes if o.outcome == "cancelled")
    failed = sum(1 for o in outcomes if o.failed)
    log("")
    log(f"Summary: {taken} taken from the monitoring table, {confirmed} confirmed, "
        f"{resent} resent, {cancelled} cancelled in Kibo, {failed} failed.")
    log(f"Log: {log_path}")
    log(f"Results CSV: {csv_path}")

    # A failed row is reported in the log; the job stays green so one bad row does
    # not look like a broken workflow.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
