#!/usr/bin/env python3
"""
The cancellation ledger: `tlg-business-intelligence-prd`.til.warehouse_orders_cancellation_table.

Every shipment this repository cancels at the warehouse gets a row here, and that
row is the only record of what happened to it afterwards. Two workers write to it:

    scripts/process_warehouse_cancellations.py
        writes the row when the CANCEL event is sent (or fails to send)

    scripts/reconcile_cancellations.py
        confirms it against the warehouse events, resends the ones the warehouse
        never acknowledged, and cancels the confirmed ones in Kibo

Columns
-------
    row_insertion_date                          TIMESTAMP  when the row was first written
    row_last_modified_date                      TIMESTAMP  when it last changed
    shipment_order_number                       STRING     e.g. OW-USA00037769
    shipment_items                              STRING     product codes joined by ';'
    cancellation_sent_to_warehouse              BOOL       did the GXO call go through
    cancellation_sent_to_warehouse_date         TIMESTAMP  when it was sent or attempted
    cancellation_confirmed_by_warehouse         BOOL       a CANCELLED event came back
    cancellation_confirmed_by_warehouse_date    TIMESTAMP  that event's eventTimestamp
    number_of_cancellations_sent_to_warehouse   INT64      1 on the first successful send
    cancelled_on_kibo                           BOOL       the Kibo shipment is cancelled
    cancelled_on_kibo_date                      TIMESTAMP  when that happened

`sql/warehouse_orders_cancellation_table.sql` adds the ones the table did not already have.
Run it once before either worker writes.

Nothing in here is allowed to break a cancellation run. Every public call returns
(ok, message) and raises nothing: a shipment that was stopped at the warehouse but
could not be written down is still a shipment that was stopped, and the run says so
in its log rather than failing.

Credentials:
    BI_BIGQUERY_CREDS   service account JSON for tlg-business-intelligence-prd
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

try:
    from google.cloud import bigquery
    from google.oauth2 import service_account
except ImportError:  # reported by the caller, which knows how to complain
    bigquery = None  # type: ignore[assignment]
    service_account = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Where the ledger lives
# ---------------------------------------------------------------------------

BI_PROJECT = "tlg-business-intelligence-prd"
BI_DATASET = "til"
CANCELLATION_TABLE = "warehouse_orders_cancellation_table"

BQ_SCOPES = ["https://www.googleapis.com/auth/bigquery"]

# The separator the shipment_items column uses. scripts of the shipments_resolver
# repository split that column on [,\s;|]+, so a ';' joined list is read by them
# unchanged.
ITEM_SEPARATOR = ";"

# Column names, resolved case-insensitively against the real schema so a table
# that spells one of them differently still works.
COLUMN_CANDIDATES: Dict[str, Tuple[str, ...]] = {
    "row_insertion_date": ("row_insertion_date", "rowInsertionDate", "insert_date"),
    "row_last_modified_date": (
        "row_last_modified_date", "rowLastModifiedDate", "last_modified_date",
    ),
    "shipment_order_number": (
        "shipment_order_number", "shipmentOrderNumber", "shipment_number",
        "shipmentNumber",
    ),
    "shipment_items": ("shipment_items", "shipmentItems", "items"),
    "cancellation_sent_to_warehouse": (
        "cancellation_sent_to_warehouse", "cancellationSentToWarehouse",
    ),
    "cancellation_sent_to_warehouse_date": (
        "cancellation_sent_to_warehouse_date", "cancellationSentToWarehouseDate",
    ),
    "cancellation_confirmed_by_warehouse": (
        "cancellation_confirmed_by_warehouse", "cancellationConfirmedByWarehouse",
    ),
    "cancellation_confirmed_by_warehouse_date": (
        "cancellation_confirmed_by_warehouse_date",
        "cancellationConfirmedByWarehouseDate",
    ),
    "number_of_cancellations_sent_to_warehouse": (
        "number_of_cancellations_sent_to_warehouse",
        "numberOfCancellationsSentToWarehouse",
    ),
    "cancelled_on_kibo": ("cancelled_on_kibo", "canceled_on_kibo", "cancelledOnKibo"),
    "cancelled_on_kibo_date": (
        "cancelled_on_kibo_date", "canceled_on_kibo_date", "cancelledOnKiboDate",
    ),
}

# The ones a worker cannot do without. The rest are reported as missing and the
# statement simply leaves them alone.
REQUIRED_COLUMNS = ("shipment_order_number", "shipment_items")

# Read a flag column whatever its type, exactly as the shipments_resolver does.
TRUTHY_FLAG_VALUES = ("TRUE", "T", "YES", "Y", "1")

# Every string on a shipment item that could name its product. The first one a
# given item carries is the one written to shipment_items, so a shipment always
# contributes one identifier per item line and the column stays comparable with
# the warehouse's own requestPayload.
ITEM_ID_FIELDS = (
    "productCode", "upc", "sku", "barcode", "ean", "variationProductCode",
    "partNumber", "itemCode", "productId",
)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def clean(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    text = str(value).strip()
    return text[:-2] if re.fullmatch(r"\d+\.0", text) else text


def normalize_id(value: Any) -> str:
    """Identifier comparison: case-insensitive and blind to spaces and hyphens."""
    return re.sub(r"[\s_\-]+", "", clean(value)).upper()


def truthy_flag(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return clean(value).upper() in TRUTHY_FLAG_VALUES


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def item_product_code(item: Any) -> str:
    """
    The one identifier that stands for a shipment item.

    Kibo names the same product several ways at once (productCode, upc, sku), and
    the warehouse names it once. Writing all of them would make shipment_items
    longer than the event it is later compared with, so the first field the item
    carries wins, in the order of ITEM_ID_FIELDS.
    """
    if not isinstance(item, dict):
        return clean(item)
    for key in ITEM_ID_FIELDS:
        text = clean(item.get(key))
        if text:
            return text
    # Kibo nests the catalogue record under 'product' on some responses.
    product = item.get("product")
    if isinstance(product, dict):
        for key in ITEM_ID_FIELDS:
            text = clean(product.get(key))
            if text:
                return text
    data = item.get("data")
    if isinstance(data, dict):
        for key in ITEM_ID_FIELDS:
            text = clean(data.get(key))
            if text:
                return text
    return ""


# Where a shipment keeps its live products, in the order they are looked for.
LIVE_ITEM_KEYS = ("items", "shipmentItems", "lineItems")
# Where they go once Kibo has moved them off the shipment. A REASSIGNED or
# CANCELED shipment comes back with an empty `items` and its products here
# instead, so a caller that has already decided the shipment is worth reading
# can ask for these as a fallback.
MOVED_ITEM_KEYS = ("reassignedItems", "canceledItems", "cancelledItems")


def shipment_item_codes(shipment: Any, include_moved: bool = False) -> List[str]:
    """
    The products of one shipment, one entry per item line.

    `items` is the only list read by default. With include_moved, a shipment
    whose `items` is empty is read from `reassignedItems` / `canceledItems`
    instead — Kibo empties `items` when it hands the goods to another shipment,
    and a caller that has already filtered by status may still want to know what
    the shipment was for.
    """
    if not isinstance(shipment, dict):
        return []
    keys = LIVE_ITEM_KEYS + (MOVED_ITEM_KEYS if include_moved else ())
    for key in keys:
        value = shipment.get(key)
        if isinstance(value, list) and value:
            return [code for code in (item_product_code(item) for item in value) if code]
    return []


def items_of_shipments(shipments: Sequence[Dict[str, Any]],
                       include_moved: bool = False) -> List[str]:
    """
    Every product on the given shipments, de-duplicated and sorted so the same
    shipments always produce the same string.

    The default leaves `reassignedItems` alone on purpose: the shipments of one
    order include the generations Kibo has already reassigned, and reading their
    moved items would put products on the ledger row that the live shipment no
    longer carries.
    """
    codes: List[str] = []
    for shipment in shipments:
        codes.extend(shipment_item_codes(shipment, include_moved))
    return sorted({code for code in codes})


def format_items(codes: Iterable[str]) -> str:
    """The shipment_items column value: 'A;B;C'."""
    return ITEM_SEPARATOR.join(sorted({clean(code) for code in codes if clean(code)}))


def parse_items(value: Any) -> List[str]:
    """
    The shipment_items column back into identifiers.

    The column has been seen as a ';' joined string, as a JSON array and as a
    repeated field, so all three are read.
    """
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        found: List[str] = []
        for entry in value:
            found.extend(parse_items(entry))
        return sorted(set(found))
    if isinstance(value, dict):
        code = item_product_code(value)
        return [code] if code else []
    text = clean(value)
    if not text:
        return []
    if text[0] in "[{":
        try:
            return parse_items(json.loads(text))
        except json.JSONDecodeError:
            pass
    return sorted({
        token.strip().strip("\"'").upper()
        for token in re.split(r"[,\s;|]+", text)
        if token.strip().strip("\"'")
    })


def same_items(left: Iterable[str], right: Iterable[str]) -> bool:
    """
    'the same items, not more and not less'.

    Identifiers are compared upper-cased and stripped of spaces and hyphens, so
    the warehouse's spelling of a code and Kibo's spelling of it still match.
    """
    return ({normalize_id(value) for value in left if normalize_id(value)}
            == {normalize_id(value) for value in right if normalize_id(value)})


# ---------------------------------------------------------------------------
# The client
# ---------------------------------------------------------------------------

def bi_bigquery_client(env_name: str = "BI_BIGQUERY_CREDS") -> Tuple[Any, str]:
    """(client, error). A non-empty error means the ledger cannot be reached."""
    if bigquery is None:
        return None, ("google-cloud-bigquery is not installed; "
                      "run pip install -r requirements.txt")
    raw = os.environ.get(env_name, "").strip().lstrip("﻿")
    if not raw:
        return None, f"{env_name} is empty"
    try:
        info = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, f"{env_name} does not contain valid JSON: {exc}"
    if not isinstance(info, dict):
        return None, f"{env_name} must contain a JSON object"
    for key in ("type", "project_id", "private_key", "client_email"):
        if not info.get(key):
            return None, f"{env_name} is missing the '{key}' field"
    try:
        credentials = service_account.Credentials.from_service_account_info(
            info, scopes=BQ_SCOPES
        )
        client = bigquery.Client(
            project=clean(info.get("project_id")) or BI_PROJECT,
            credentials=credentials,
        )
    except Exception as exc:  # noqa: BLE001  the caller degrades, it does not die
        return None, f"could not build the BigQuery client: {exc}"
    return client, ""


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

@dataclass
class Schema:
    """The ledger's real column names and types, resolved once per run."""
    names: Dict[str, str] = field(default_factory=dict)   # logical -> actual
    types: Dict[str, str] = field(default_factory=dict)   # actual (lower) -> type
    missing: List[str] = field(default_factory=list)
    error: str = ""

    def has(self, logical: str) -> bool:
        return logical in self.names

    def column(self, logical: str) -> str:
        return self.names.get(logical, "")

    def type_of(self, logical: str) -> str:
        return self.types.get(self.names.get(logical, "").lower(), "").upper()

    def bool_literal(self, logical: str, value: bool) -> str:
        """TRUE / 'true' / 1, whichever the column's own type accepts."""
        kind = self.type_of(logical)
        if kind.startswith("STRING"):
            return "'true'" if value else "'false'"
        if kind.startswith(("INT", "NUMERIC", "BIGNUMERIC", "FLOAT")):
            return "1" if value else "0"
        return "TRUE" if value else "FALSE"

    def is_truthy_sql(self, logical: str) -> str:
        """A WHERE clause reading a flag column of any type as a boolean."""
        column = self.column(logical)
        truthy = ", ".join(f"'{word}'" for word in TRUTHY_FLAG_VALUES)
        return f"IFNULL(UPPER(TRIM(CAST(`{column}` AS STRING))), '') IN ({truthy})"


def load_schema(client: Any) -> Schema:
    sql = f"""
        SELECT column_name, data_type
        FROM `{BI_PROJECT}.{BI_DATASET}`.INFORMATION_SCHEMA.COLUMNS
        WHERE table_name = @table
    """
    schema = Schema()
    try:
        job = client.query(
            sql,
            job_config=bigquery.QueryJobConfig(query_parameters=[
                bigquery.ScalarQueryParameter("table", "STRING", CANCELLATION_TABLE),
            ]),
        )
        actual = {clean(row["column_name"]): clean(row["data_type"]) for row in job.result()}
    except Exception as exc:  # noqa: BLE001
        schema.error = f"could not read the schema of {CANCELLATION_TABLE}: {exc}"
        return schema

    if not actual:
        schema.error = (
            f"{BI_PROJECT}.{BI_DATASET}.{CANCELLATION_TABLE} does not exist, or the "
            "service account cannot see it"
        )
        return schema

    lowered = {name.lower(): name for name in actual}
    schema.types = {name.lower(): kind for name, kind in actual.items()}
    for logical, candidates in COLUMN_CANDIDATES.items():
        for candidate in candidates:
            found = lowered.get(candidate.lower())
            if found:
                schema.names[logical] = found
                break
        else:
            schema.missing.append(logical)

    required_missing = [name for name in REQUIRED_COLUMNS if name in schema.missing]
    if required_missing:
        schema.error = (
            f"{CANCELLATION_TABLE} has no {', '.join(required_missing)} column; "
            "run sql/warehouse_orders_cancellation_table.sql"
        )
    return schema


# ---------------------------------------------------------------------------
# Writing: the cancellation run
# ---------------------------------------------------------------------------

@dataclass
class LedgerEntry:
    """One shipment, as the warehouse worker leaves it."""
    shipment_order_number: str
    items: str = ""
    sent_to_warehouse: bool = False
    sent_at: Optional[datetime] = None
    cancelled_on_kibo: bool = False
    cancelled_on_kibo_at: Optional[datetime] = None


class CancellationLedger:
    """
    The ledger, as the warehouse worker uses it: build it once, call record()
    per shipment, read .errors afterwards.

    Nothing raises. `available` is False when the table could not be reached at
    all, and every record() then returns the same explanation so the run's log
    carries it once per shipment rather than stopping.
    """

    def __init__(self, client: Any = None, schema: Optional[Schema] = None,
                 error: str = "", dry_run: bool = False) -> None:
        self.client = client
        self.schema = schema or Schema()
        self.error = error or self.schema.error
        self.dry_run = dry_run
        self.errors: List[str] = []
        self.written = 0

    @property
    def available(self) -> bool:
        return bool(self.client) and not self.error

    @classmethod
    def build(cls, dry_run: bool = False,
              env_name: str = "BI_BIGQUERY_CREDS") -> "CancellationLedger":
        client, error = bi_bigquery_client(env_name)
        if error:
            return cls(error=f"the cancellation ledger is unreachable: {error}",
                       dry_run=dry_run)
        schema = load_schema(client)
        if schema.error:
            return cls(client=client, schema=schema, error=schema.error, dry_run=dry_run)
        return cls(client=client, schema=schema, dry_run=dry_run)

    # -- the statement ------------------------------------------------------

    def record(self, entry: LedgerEntry) -> Tuple[bool, str]:
        """
        Write one shipment: insert the row, or update the one already there.

        The key is shipment_order_number. A shipment cancelled twice does not get
        a second row — it gets its counter raised, which is what
        number_of_cancellations_sent_to_warehouse is for.

        Returns (ok, message). A False never means the cancellation failed, only
        that it was not written down.
        """
        if not self.available:
            return False, self.error
        number = clean(entry.shipment_order_number)
        if not number:
            return False, "the shipment has no shipmentOrderNumber to key the row on"

        when = entry.sent_at or now_utc()
        schema = self.schema
        son_col = schema.column("shipment_order_number")
        items_col = schema.column("shipment_items")

        # Column lists are built from what the table actually has, so a ledger
        # still missing one of the newer columns is updated as far as it goes
        # rather than refused.
        updates: List[str] = []
        insert_columns: List[str] = [f"`{son_col}`", f"`{items_col}`"]
        insert_values: List[str] = ["@son", "@items"]

        def add(logical: str, update_sql: str, insert_sql: str) -> None:
            if not schema.has(logical):
                return
            column = schema.column(logical)
            updates.append(f"`{column}` = {update_sql}")
            insert_columns.append(f"`{column}`")
            insert_values.append(insert_sql)

        # shipment_items is only overwritten when this run actually read some:
        # a run that could not reach Kibo must not blank out what an earlier one
        # established.
        updates.append(f"`{items_col}` = IF(@items != '', @items, `{items_col}`)")

        add("row_last_modified_date", "@now", "@now")
        # row_insertion_date is set on insert and never touched again.
        if schema.has("row_insertion_date"):
            insert_columns.append(f"`{schema.column('row_insertion_date')}`")
            insert_values.append("@now")

        add("cancellation_sent_to_warehouse",
            schema.bool_literal("cancellation_sent_to_warehouse", entry.sent_to_warehouse),
            schema.bool_literal("cancellation_sent_to_warehouse", entry.sent_to_warehouse))
        add("cancellation_sent_to_warehouse_date", "@sent_at", "@sent_at")

        if schema.has("number_of_cancellations_sent_to_warehouse"):
            counter = schema.column("number_of_cancellations_sent_to_warehouse")
            step = 1 if entry.sent_to_warehouse else 0
            updates.append(
                f"`{counter}` = IFNULL(`{counter}`, 0) + {step}"
            )
            insert_columns.append(f"`{counter}`")
            insert_values.append(str(step))

        # A fresh attempt is not confirmed yet; the reconciler sets these.
        if schema.has("cancellation_confirmed_by_warehouse"):
            insert_columns.append(f"`{schema.column('cancellation_confirmed_by_warehouse')}`")
            insert_values.append(
                schema.bool_literal("cancellation_confirmed_by_warehouse", False)
            )
        if schema.has("cancellation_confirmed_by_warehouse_date"):
            insert_columns.append(
                f"`{schema.column('cancellation_confirmed_by_warehouse_date')}`"
            )
            insert_values.append("CAST(NULL AS TIMESTAMP)")

        # cancelled_on_kibo is false unless this very run cancelled in Kibo too
        # (the --allow-kibo path), in which case the row says so straight away
        # and the reconciler leaves it alone.
        if schema.has("cancelled_on_kibo"):
            literal = schema.bool_literal("cancelled_on_kibo", entry.cancelled_on_kibo)
            insert_columns.append(f"`{schema.column('cancelled_on_kibo')}`")
            insert_values.append(literal)
            if entry.cancelled_on_kibo:
                updates.append(f"`{schema.column('cancelled_on_kibo')}` = {literal}")
        if schema.has("cancelled_on_kibo_date"):
            column = schema.column("cancelled_on_kibo_date")
            insert_columns.append(f"`{column}`")
            insert_values.append("@kibo_at" if entry.cancelled_on_kibo
                                 else "CAST(NULL AS TIMESTAMP)")
            if entry.cancelled_on_kibo:
                updates.append(f"`{column}` = @kibo_at")

        sql = f"""
            MERGE `{BI_PROJECT}.{BI_DATASET}.{CANCELLATION_TABLE}` AS t
            USING (SELECT @son AS son) AS s
            ON UPPER(TRIM(CAST(t.`{son_col}` AS STRING))) = UPPER(TRIM(s.son))
            WHEN MATCHED THEN UPDATE SET
                {", ".join(updates)}
            WHEN NOT MATCHED THEN INSERT ({", ".join(insert_columns)})
                VALUES ({", ".join(insert_values)})
        """

        if self.dry_run:
            return True, (f"would write {number} to {CANCELLATION_TABLE} "
                          f"(items={entry.items or '-'}, "
                          f"sent={entry.sent_to_warehouse})")

        parameters = [
            bigquery.ScalarQueryParameter("son", "STRING", number),
            bigquery.ScalarQueryParameter("items", "STRING", clean(entry.items)),
            bigquery.ScalarQueryParameter("now", "TIMESTAMP", now_utc()),
            bigquery.ScalarQueryParameter("sent_at", "TIMESTAMP", when),
            bigquery.ScalarQueryParameter(
                "kibo_at", "TIMESTAMP", entry.cancelled_on_kibo_at or when
            ),
        ]
        try:
            job = self.client.query(
                sql, job_config=bigquery.QueryJobConfig(query_parameters=parameters)
            )
            job.result()
        except Exception as exc:  # noqa: BLE001  the shipment is already cancelled
            message = f"{number}: could not write to {CANCELLATION_TABLE}: {exc}"
            self.errors.append(message)
            return False, message
        self.written += 1
        return True, f"{number} written to {CANCELLATION_TABLE}"


# ---------------------------------------------------------------------------
# Reading and updating: the reconciliation run
# ---------------------------------------------------------------------------

@dataclass
class LedgerRow:
    """One row of the ledger, as the reconciler reads it."""
    shipment_order_number: str = ""
    items: List[str] = field(default_factory=list)
    raw_items: str = ""
    sent_to_warehouse: bool = False
    sent_at: Optional[datetime] = None
    confirmed_by_warehouse: bool = False
    confirmed_at: Optional[datetime] = None
    sends: int = 0
    cancelled_on_kibo: bool = False

    @property
    def label(self) -> str:
        return f"{self.shipment_order_number} [{self.raw_items or 'no items'}]"


def _select(schema: Schema, logical: str, cast: str, alias: str) -> str:
    if schema.has(logical):
        return f"CAST(`{schema.column(logical)}` AS {cast}) AS {alias}"
    return f"CAST(NULL AS {cast}) AS {alias}"


def fetch_rows(client: Any, schema: Schema, where: str,
               parameters: Optional[List[Any]] = None) -> Tuple[List[LedgerRow], str]:
    """The ledger rows matching a WHERE clause, as LedgerRow objects."""
    son_col = schema.column("shipment_order_number")
    items_col = schema.column("shipment_items")
    sql = f"""
        SELECT
            CAST(`{son_col}` AS STRING) AS shipment_order_number,
            `{items_col}` AS shipment_items,
            {_select(schema, 'cancellation_sent_to_warehouse', 'STRING', 'sent')},
            {_select(schema, 'cancellation_sent_to_warehouse_date', 'TIMESTAMP', 'sent_at')},
            {_select(schema, 'cancellation_confirmed_by_warehouse', 'STRING', 'confirmed')},
            {_select(schema, 'cancellation_confirmed_by_warehouse_date', 'TIMESTAMP',
                     'confirmed_at')},
            {_select(schema, 'number_of_cancellations_sent_to_warehouse', 'INT64', 'sends')},
            {_select(schema, 'cancelled_on_kibo', 'STRING', 'cancelled_on_kibo')}
        FROM `{BI_PROJECT}.{BI_DATASET}.{CANCELLATION_TABLE}`
        WHERE `{son_col}` IS NOT NULL
          AND TRIM(CAST(`{son_col}` AS STRING)) != ''
          AND ({where})
    """
    try:
        job = client.query(
            sql,
            job_config=bigquery.QueryJobConfig(query_parameters=parameters or []),
        )
        rows = list(job.result())
    except Exception as exc:  # noqa: BLE001
        return [], f"could not read {CANCELLATION_TABLE}: {exc}"

    found: List[LedgerRow] = []
    for row in rows:
        number = clean(row["shipment_order_number"])
        if not number:
            continue
        items = parse_items(row["shipment_items"])
        found.append(LedgerRow(
            shipment_order_number=number,
            items=items,
            raw_items=format_items(items),
            sent_to_warehouse=truthy_flag(row["sent"]),
            sent_at=row["sent_at"],
            confirmed_by_warehouse=truthy_flag(row["confirmed"]),
            confirmed_at=row["confirmed_at"],
            sends=int(row["sends"] or 0),
            cancelled_on_kibo=truthy_flag(row["cancelled_on_kibo"]),
        ))
    return found, ""


def rows_awaiting_confirmation(client: Any, schema: Schema) -> Tuple[List[LedgerRow], str]:
    """sent to the warehouse, not confirmed by it yet."""
    for logical in ("cancellation_sent_to_warehouse",
                    "cancellation_confirmed_by_warehouse"):
        if not schema.has(logical):
            return [], f"{CANCELLATION_TABLE} has no {logical} column"
    return fetch_rows(
        client, schema,
        f"{schema.is_truthy_sql('cancellation_sent_to_warehouse')} "
        f"AND NOT {schema.is_truthy_sql('cancellation_confirmed_by_warehouse')}",
    )


def rows_confirmed_not_cancelled(client: Any, schema: Schema) -> Tuple[List[LedgerRow], str]:
    """confirmed by the warehouse, still open in Kibo."""
    if not schema.has("cancellation_confirmed_by_warehouse"):
        return [], f"{CANCELLATION_TABLE} has no cancellation_confirmed_by_warehouse column"
    where = schema.is_truthy_sql("cancellation_confirmed_by_warehouse")
    if schema.has("cancelled_on_kibo"):
        where += f" AND NOT {schema.is_truthy_sql('cancelled_on_kibo')}"
    return fetch_rows(client, schema, where)


def _update(client: Any, sql: str, parameters: List[Any]) -> Tuple[int, str]:
    try:
        job = client.query(
            sql, job_config=bigquery.QueryJobConfig(query_parameters=parameters)
        )
        job.result()
    except Exception as exc:  # noqa: BLE001
        return 0, str(exc)
    return int(job.num_dml_affected_rows or 0), ""


def _key_clause(schema: Schema) -> str:
    son_col = schema.column("shipment_order_number")
    return f"UPPER(TRIM(CAST(`{son_col}` AS STRING))) = UPPER(TRIM(@son))"


def mark_confirmed(client: Any, schema: Schema, shipment_order_number: str,
                   confirmed_at: Optional[datetime]) -> Tuple[int, str]:
    """
    cancellation_confirmed_by_warehouse = true, with the CANCELLED event's own
    eventTimestamp as the date.
    """
    sets = [
        f"`{schema.column('cancellation_confirmed_by_warehouse')}` = "
        f"{schema.bool_literal('cancellation_confirmed_by_warehouse', True)}"
    ]
    if schema.has("cancellation_confirmed_by_warehouse_date"):
        sets.append(
            f"`{schema.column('cancellation_confirmed_by_warehouse_date')}` = @confirmed_at"
        )
    if schema.has("row_last_modified_date"):
        sets.append(f"`{schema.column('row_last_modified_date')}` = @now")
    sql = f"""
        UPDATE `{BI_PROJECT}.{BI_DATASET}.{CANCELLATION_TABLE}`
        SET {", ".join(sets)}
        WHERE {_key_clause(schema)}
    """
    return _update(client, sql, [
        bigquery.ScalarQueryParameter("son", "STRING", clean(shipment_order_number)),
        bigquery.ScalarQueryParameter("confirmed_at", "TIMESTAMP",
                                      confirmed_at or now_utc()),
        bigquery.ScalarQueryParameter("now", "TIMESTAMP", now_utc()),
    ])


def record_resend(client: Any, schema: Schema,
                  shipment_order_number: str) -> Tuple[int, str]:
    """
    One more cancellation went out: raise the counter and move the sent date to now.
    """
    sets: List[str] = []
    if schema.has("number_of_cancellations_sent_to_warehouse"):
        counter = schema.column("number_of_cancellations_sent_to_warehouse")
        sets.append(f"`{counter}` = IFNULL(`{counter}`, 0) + 1")
    if schema.has("cancellation_sent_to_warehouse"):
        sets.append(
            f"`{schema.column('cancellation_sent_to_warehouse')}` = "
            f"{schema.bool_literal('cancellation_sent_to_warehouse', True)}"
        )
    if schema.has("cancellation_sent_to_warehouse_date"):
        sets.append(f"`{schema.column('cancellation_sent_to_warehouse_date')}` = @now")
    if schema.has("row_last_modified_date"):
        sets.append(f"`{schema.column('row_last_modified_date')}` = @now")
    if not sets:
        return 0, f"{CANCELLATION_TABLE} has no column to record a resend in"
    sql = f"""
        UPDATE `{BI_PROJECT}.{BI_DATASET}.{CANCELLATION_TABLE}`
        SET {", ".join(sets)}
        WHERE {_key_clause(schema)}
    """
    return _update(client, sql, [
        bigquery.ScalarQueryParameter("son", "STRING", clean(shipment_order_number)),
        bigquery.ScalarQueryParameter("now", "TIMESTAMP", now_utc()),
    ])


def mark_cancelled_on_kibo(client: Any, schema: Schema,
                           shipment_order_number: str) -> Tuple[int, str]:
    """cancelled_on_kibo = true, cancelled_on_kibo_date = now."""
    if not schema.has("cancelled_on_kibo"):
        return 0, f"{CANCELLATION_TABLE} has no cancelled_on_kibo column"
    sets = [
        f"`{schema.column('cancelled_on_kibo')}` = "
        f"{schema.bool_literal('cancelled_on_kibo', True)}"
    ]
    if schema.has("cancelled_on_kibo_date"):
        sets.append(f"`{schema.column('cancelled_on_kibo_date')}` = @now")
    if schema.has("row_last_modified_date"):
        sets.append(f"`{schema.column('row_last_modified_date')}` = @now")
    sql = f"""
        UPDATE `{BI_PROJECT}.{BI_DATASET}.{CANCELLATION_TABLE}`
        SET {", ".join(sets)}
        WHERE {_key_clause(schema)}
    """
    return _update(client, sql, [
        bigquery.ScalarQueryParameter("son", "STRING", clean(shipment_order_number)),
        bigquery.ScalarQueryParameter("now", "TIMESTAMP", now_utc()),
    ])
