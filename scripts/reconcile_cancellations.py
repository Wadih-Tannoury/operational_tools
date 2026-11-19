#!/usr/bin/env python3
"""
Follow up on the cancellations already sent to the warehouse.

This worker runs on a schedule and reads nothing but the ledger,
`tlg-business-intelligence-prd`.til.warehouse_orders_cancellation_table, which
scripts/process_warehouse_cancellations.py fills as it cancels. It never touches
warehouse/input/ and shares no state with that worker beyond the table, so the two
run independently.

Three stages, in this order.

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
from typing import Any, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

import cancellation_log  # noqa: E402
from cancellation_log import (  # noqa: E402
    CANCELLATION_TABLE, LedgerRow, Schema, bi_bigquery_client, clean, format_items,
    load_schema, mark_cancelled_on_kibo, mark_confirmed, normalize_id, now_utc,
    parse_items, record_resend, rows_awaiting_confirmation, rows_confirmed_not_cancelled,
    same_items,
)

# The warehouse worker owns the GXO client, the Kibo client and the brand/tenant
# map. They are imported rather than copied so the two workers cannot drift apart
# on how a CANCEL event is built or how Kibo is talked to.
from process_warehouse_cancellations import (  # noqa: E402
    BRAND_TENANTS, DEFAULT_GXO_URL, DEFAULT_KIBO_AUTH_URL, DEFAULT_KIBO_HOST,
    DEFAULT_REASON_MESSAGE, DEFAULT_UPDATE_REASON, GxoClient, KiboClient, OrderRow,
    brand_prefix, gxo_api_key, is_terminal_status, kibo_credentials,
    load_tenant_map, shipment_items, shipment_number_of, shipment_order_number_of,
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
    stage: str = ""                 # confirm | resend | kibo
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
        updated, update_error = mark_confirmed(bi_client, schema,
                                               row.shipment_order_number, event.timestamp)
        if update_error:
            still_open.append(row)
            outcomes.append(Outcome(**base, outcome="failed",
                                    detail=f"could not write the confirmation: {update_error}"))
            continue
        outcomes.append(Outcome(
            **base, outcome="confirmed",
            detail=f"{updated} row(s) confirmed from the CANCELLED event of "
                   f"{event.timestamp.isoformat() if event.timestamp else 'unknown date'}",
        ))
    return outcomes, still_open


# ---------------------------------------------------------------------------
# Stage 2
# ---------------------------------------------------------------------------

def run_resend(bi_client: Any, schema: Schema, rows: Sequence[LedgerRow],
               facts: Dict[str, OrderFacts], gxo: Optional[GxoClient],
               args: argparse.Namespace) -> List[Outcome]:
    """Send the CANCEL event again for everything the warehouse never acknowledged."""
    outcomes: List[Outcome] = []
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

        updated, update_error = record_resend(bi_client, schema, row.shipment_order_number)
        outcomes.append(Outcome(
            **base, outcome="sent",
            order_number=entry.order_number if entry else "",
            event_id=", ".join(filter(None, event_ids)),
            detail=(f"resent, attempt {row.sends + 1}; " + "; ".join(details)[:300]
                    + (f"; the ledger was not updated: {update_error}"
                       if update_error else f"; {updated} row(s) updated")),
        ))
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
        shipments, read_error = kibo.get_shipments_by_external_order_id(
            tenant_id, site_id, order_number
        )
        if read_error:
            outcomes.append(Outcome(
                **base, outcome="failed", order_number=order_number, tenant_id=tenant_id,
                detail=f"getShipments by externalOrderId '{order_number}' failed: {read_error}",
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
            ok, send_error, excerpt = kibo.cancel_shipment(
                tenant_id, site_id, shipment_number, args.kibo_reason_code,
                args.kibo_more_info, args.kibo_send_notifications,
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

        updated, update_error = mark_cancelled_on_kibo(bi_client, schema,
                                                       row.shipment_order_number)
        outcomes.append(Outcome(
            **shared, outcome="cancelled",
            detail=(f"{note}; cancelled {', '.join(cancelled)}"
                    + (f"; also tried {'; '.join(problems)}"[:200] if problems else "")
                    + (f"; the ledger was not updated: {update_error}" if update_error
                       else f"; {updated} ledger row(s) updated")),
        ))
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
        outcomes.extend(run_kibo(bi_client, schema, kibo, facts,
                                 load_tenant_map(args.tenant_map), args))
    else:
        log("[kibo] nothing is confirmed and still open.")

    for outcome in outcomes:
        log(f"[{outcome.stage}] {outcome.outcome.upper()} "
            f"{outcome.shipment_order_number or '-'}"
            + (f" :: {outcome.detail}" if outcome.detail else ""))

    log_path, csv_path = write_outputs(args.log_dir, stamp, outcomes, args, started_at)
    confirmed = sum(1 for o in outcomes if o.outcome == "confirmed")
    resent = sum(1 for o in outcomes if o.outcome == "sent")
    cancelled = sum(1 for o in outcomes if o.outcome == "cancelled")
    failed = sum(1 for o in outcomes if o.failed)
    log("")
    log(f"Summary: {confirmed} confirmed, {resent} resent, {cancelled} cancelled in Kibo, "
        f"{failed} failed.")
    log(f"Log: {log_path}")
    log(f"Results CSV: {csv_path}")

    # A failed row is reported in the log; the job stays green so one bad row does
    # not look like a broken workflow.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
