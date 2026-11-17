#!/usr/bin/env python3
"""
Consume the cancellation CSV files dropped in input/ and apply them to Kibo.

CSV columns (header matching is case / space / underscore insensitive):

    shipmentOrderNumber   e.g. OW-USA00037769          optional
    productId             productCode / UPC / SKU      optional
    orderNumber           e.g. OW-US-00037769          optional
    quantity              items only, default = all    optional
    reasonCode            overrides --reason-code      optional
    moreInfo              overrides --more-info        optional
    tenantId / siteId     override the defaults        optional

Resolution rules
----------------
1. shipmentOrderNumber + productId
       Look the shipmentOrderNumber up in
       `tlg-business-intelligence-prd`.til.oms_active_shipments (column
       shipment_order_number). When several rows match, keep the one whose
       shipment_json contains that productId. Cancel ONLY that item, with
           PUT /api/commerce/shipments/{oms_shipment_number}/canceledItems

2. shipmentOrderNumber, productId empty
       Same lookup, then cancel every oms_shipment_number found, with
           PUT /api/commerce/shipments/{oms_shipment_number}/canceled

3. shipmentOrderNumber empty, orderNumber populated
       Look the orderNumber up in the same table on external_order_number,
       then cancel every oms_shipment_number found (or, when a productId is
       given too, cancel that item on the shipment that carries it).

4. Nothing found in BigQuery
       Fall back to Kibo:
           GET /api/commerce/shipments?filter=externalOrderId=={orderNumber}
       The response carries shipmentNumber, tenantId, siteId and the items,
       which is everything the cancellation needs.

       When the row only carries a shipmentOrderNumber, the externalOrderId is
       derived from it by replacing the 6th character with a hyphen
       ("OW-USA00037769" -> "OW-US-00037769"), which is how the two numbers
       line up in this tenant. Turn that off with --no-derive-external-order-id.

Tenant resolution
-----------------
    1. a tenantId column on the CSV row
    2. tenant_id from oms_active_shipments
    3. the two-letter brand prefix of the number, through BRAND_TENANTS
       ("OW-USA00037769" and "OW-US-00037769" are both OW -> 30016)
    4. --default-tenant-id, which is empty by default

An unknown prefix with no default fails the record rather than cancelling
against the wrong brand. Add brands with KIBO_TENANT_MAP or --tenant-map.

Credentials come from environment variables (GitHub repository secrets):
    BI_BIGQUERY_CREDS   service account JSON for tlg-business-intelligence-prd
    KIBO_API_CREDS      {"client_id": "...", "client_secret": "..."}

Outputs
-------
    logs/run-<stamp>.log   human readable, one line per record
    logs/run-<stamp>.csv   the same records, structured
    archive/<stamp>__<name>.csv   every record of the file succeeded or was skipped
    error/<stamp>__<name>.csv     the file could not be read, or a record failed
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import random
import re
import shutil
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import quote

import requests

try:
    from google.cloud import bigquery
    from google.oauth2 import service_account
except ImportError:  # the script still runs Kibo-only when BigQuery is absent
    bigquery = None  # type: ignore[assignment]
    service_account = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BI_PROJECT = "tlg-business-intelligence-prd"
BI_DATASET = "til"
BI_TABLE = "oms_active_shipments"

DEFAULT_HOST = "tp3.mozu.com"
DEFAULT_AUTH_URL = "https://home.mozu.com/api/platform/applications/authtickets/oauth"

# The tenant is decided by the two-letter brand prefix that opens every
# shipmentOrderNumber and orderNumber ("OW-USA00037769" and "OW-US-00037769"
# are both tenant 30016). Several brands share a tenant, which is expected.
# Extend or override this without touching the code by setting the
# KIBO_TENANT_MAP environment variable to a JSON object, or by passing
# --tenant-map; those entries are merged over the ones below.
BRAND_TENANTS: Dict[str, str] = {
    "DG": "29916",
    "LN": "29917",
    "MO": "29988",
    "SC": "29989",
    "CO": "29990",
    "RC": "29990",
    "WL": "29991",
    "SW": "29992",
    "CA": "29993",
    "CV": "30014",
    "HB": "30014",
    "BA": "30014",
    "RL": "30016",
    "OW": "30016",
    "FA": "30017",
    "VW": "30017",
    "DJ": "30018",
    "AT": "30019",
    "LA": "30019",
    "PL": "30020",
    "CL": "30020",
    "HE": "30021",
    "FE": "30022",
    "CR": "30022",
    "FO": "30023",
    "EF": "30023",
    "PJ": "30475",
    "FU": "32145",
    "WF": "32147",
    "ZH": "32147",
    "VE": "32147",
    "BO": "33692",
    "AL": "33693",
    "MA": "33693",
    "PA": "33693",
    "FL": "33693",
    "BB": "33694",
    "TM": "54516",
}

# No tenant-wide default: guessing one would send a cancellation to the wrong
# brand. A record whose prefix is unknown is reported as failed instead.
DEFAULT_TENANT_ID = ""
DEFAULT_SITE_ID = ""

DEFAULT_REASON_CODE = "CustomerChangedTheirMind"
DEFAULT_MORE_INFO = "Unspecified"

BQ_SCOPES = ["https://www.googleapis.com/auth/bigquery"]
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

# Kibo statuses that mean there is nothing left to cancel.
TERMINAL_STATUSES = {
    "CANCELED", "CANCELLED", "CLOSED", "COMPLETE", "COMPLETED",
    "FULFILLED", "SHIPPED", "PICKEDUP", "PICKED_UP", "DELETED",
}

# Candidate column names in oms_active_shipments, resolved case-insensitively.
SON_CANDIDATES = ("shipment_order_number", "shipmentOrderNumber")
EXTERNAL_ORDER_CANDIDATES = (
    "external_order_number", "externalOrderNumber",
    "external_order_id", "externalOrderId",
    "order_number", "orderNumber",
)
OMS_NUMBER_CANDIDATES = (
    "oms_shipment_number", "omsShipmentNumber", "shipment_number", "shipmentNumber",
)
TENANT_CANDIDATES = ("tenant_id", "tenantId")
SITE_CANDIDATES = ("site_id", "siteId")
JSON_CANDIDATES = ("shipment_json", "shipmentJson", "shipment_payload")
STATUS_CANDIDATES = ("shipment_status", "shipmentStatus", "status")

# Item fields compared against the CSV productId.
ITEM_ID_FIELDS = (
    "productCode", "upc", "sku", "variationProductCode",
    "partNumber", "originalOrderItemId",
)

# Outcomes that are not failures: nothing was wrong, there was just nothing to do.
SKIP_OUTCOMES = {"already_terminal", "item_already_canceled", "dry_run"}


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


def clean(value: Any) -> str:
    """Trim a value to a string, without Excel's trailing '.0' on whole numbers."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    text = str(value).strip()
    return text[:-2] if re.fullmatch(r"\d+\.0", text) else text


def normalize_header(value: Any) -> str:
    return re.sub(r"[\s_\-]+", "", str(value or "").strip().lower())


def normalize_id(value: Any) -> str:
    """Identifier comparison: case-insensitive and blind to spaces and hyphens."""
    return re.sub(r"[\s_\-]+", "", clean(value)).upper()


def excerpt_of(response: Any, max_chars: int = 600) -> str:
    if response is None:
        return ""
    text = getattr(response, "text", "") or ""
    return re.sub(r"\s+", " ", text).strip()[:max_chars]


def retry_sleep_seconds(attempt: int, response: Optional[Any] = None) -> float:
    if response is not None:
        retry_after = getattr(response, "headers", {}).get("Retry-After")
        if retry_after:
            try:
                return min(60.0, max(0.0, float(retry_after)))
            except ValueError:
                pass
    return min(30.0, (2 ** (attempt - 1)) + random.random())


def is_terminal_status(status: str) -> bool:
    return status.upper().replace(" ", "").replace("-", "_") in TERMINAL_STATUSES


def stamp_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def brand_prefix(*values: str) -> str:
    """The two-letter brand code opening the first value that has one."""
    for value in values:
        text = clean(value).upper()
        if len(text) >= 2 and text[:2].isalpha():
            return text[:2]
    return ""


def load_tenant_map(extra: str = "") -> Dict[str, str]:
    """BRAND_TENANTS, with KIBO_TENANT_MAP and --tenant-map merged over it."""
    tenants = dict(BRAND_TENANTS)
    for source, raw in (("KIBO_TENANT_MAP", os.environ.get("KIBO_TENANT_MAP", "")),
                        ("--tenant-map", extra)):
        raw = clean(raw)
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            fail(f"{source} is not valid JSON: {exc}")
            return tenants
        if not isinstance(parsed, dict):
            fail(f"{source} must be a JSON object of {{\"BRAND\": \"tenantId\"}}.")
        for prefix, tenant in parsed.items():
            tenants[clean(prefix).upper()] = clean(tenant)
    return tenants


def derive_external_order_id(shipment_order_number: str) -> str:
    """
    'OW-USA00037769' -> 'OW-US-00037769'.

    The shipmentOrderNumber carries a site letter in 6th position where the
    externalOrderId carries a hyphen. Returns '' when the number is too short.
    """
    value = clean(shipment_order_number)
    if len(value) < 7:
        return ""
    return value[:5] + "-" + value[6:]


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------

def load_json_env(name: str) -> Dict[str, Any]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        fail(f"Environment variable {name} is empty. Check the repository secret.")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        fail(f"Environment variable {name} does not contain valid JSON: {exc}")
    if not isinstance(data, dict):
        fail(f"Environment variable {name} must contain a JSON object.")
    return data


def kibo_credentials() -> Dict[str, str]:
    data = load_json_env("KIBO_API_CREDS")
    client_id = clean(data.get("client_id"))
    client_secret = clean(data.get("client_secret"))
    if not client_id or not client_secret:
        fail("KIBO_API_CREDS must carry a non-empty client_id and client_secret.")
    return {"client_id": client_id, "client_secret": client_secret}


def bigquery_client() -> Optional["bigquery.Client"]:
    if bigquery is None:
        warn("google-cloud-bigquery is not installed; every record will go through the Kibo fallback.")
        return None
    raw = os.environ.get("BI_BIGQUERY_CREDS", "").strip()
    if not raw:
        warn("BI_BIGQUERY_CREDS is empty; every record will go through the Kibo fallback.")
        return None
    info = load_json_env("BI_BIGQUERY_CREDS")
    for key in ("type", "project_id", "private_key", "client_email"):
        if not info.get(key):
            fail(f"BI_BIGQUERY_CREDS is missing the '{key}' field.")
    project = clean(info.get("project_id")) or BI_PROJECT
    credentials = service_account.Credentials.from_service_account_info(info, scopes=BQ_SCOPES)
    return bigquery.Client(project=project, credentials=credentials)


# ---------------------------------------------------------------------------
# CSV input
# ---------------------------------------------------------------------------

@dataclass
class InputRecord:
    source_file: str
    row_number: int
    shipment_order_number: str = ""
    product_id: str = ""
    order_number: str = ""
    quantity: str = ""
    reason_code: str = ""
    more_info: str = ""
    tenant_id: str = ""
    site_id: str = ""

    @property
    def label(self) -> str:
        parts = []
        if self.shipment_order_number:
            parts.append(f"shipmentOrderNumber={self.shipment_order_number}")
        if self.order_number:
            parts.append(f"orderNumber={self.order_number}")
        if self.product_id:
            parts.append(f"productId={self.product_id}")
        return " ".join(parts) or "(empty row)"


COLUMN_ALIASES = {
    "shipmentordernumber": "shipment_order_number",
    "shipmentorder": "shipment_order_number",
    "son": "shipment_order_number",
    "productid": "product_id",
    "product": "product_id",
    "productcode": "product_id",
    "sku": "product_id",
    "upc": "product_id",
    "ordernumber": "order_number",
    "externalordernumber": "order_number",
    "externalorderid": "order_number",
    "quantity": "quantity",
    "qty": "quantity",
    "reasoncode": "reason_code",
    "reason": "reason_code",
    "moreinfo": "more_info",
    "tenantid": "tenant_id",
    "siteid": "site_id",
}


def read_csv_records(path: Path) -> Tuple[List[InputRecord], str]:
    """Return (records, error). A non-empty error means the file is unusable."""
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = path.read_text(encoding="latin-1")
        except Exception as exc:  # noqa: BLE001
            return [], f"could not decode the file: {exc}"
    except Exception as exc:  # noqa: BLE001
        return [], f"could not read the file: {exc}"

    if not text.strip():
        return [], "the file is empty"

    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel  # type: ignore[assignment]

    reader = csv.reader(io.StringIO(text), dialect)
    try:
        header = next(reader)
    except StopIteration:
        return [], "the file has no header row"

    mapping: Dict[int, str] = {}
    for index, name in enumerate(header):
        field_name = COLUMN_ALIASES.get(normalize_header(name))
        if field_name and index not in mapping:
            mapping[index] = field_name

    known = set(mapping.values())
    if not ({"shipment_order_number", "order_number", "product_id"} & known):
        return [], (
            "none of the expected columns were found. The header needs at least one of "
            "shipmentOrderNumber, orderNumber or productId. Found: "
            + ", ".join(str(name) for name in header)
        )

    records: List[InputRecord] = []
    for row_number, row in enumerate(reader, start=2):
        if not row or all(not clean(cell) for cell in row):
            continue
        record = InputRecord(source_file=path.name, row_number=row_number)
        for index, field_name in mapping.items():
            if index < len(row):
                setattr(record, field_name, clean(row[index]))
        if not (record.shipment_order_number or record.order_number):
            record.quantity = record.quantity  # keep the row, it is reported as invalid
        records.append(record)

    if not records:
        return [], "the file has a header but no data rows"
    return records, ""


# ---------------------------------------------------------------------------
# BigQuery lookup
# ---------------------------------------------------------------------------

@dataclass
class OmsRow:
    shipment_order_number: str = ""
    external_order_number: str = ""
    oms_shipment_number: str = ""
    tenant_id: str = ""
    site_id: str = ""
    shipment_status: str = ""
    shipment_json: str = ""


def table_columns(client: "bigquery.Client") -> Dict[str, str]:
    sql = (
        f"SELECT column_name FROM `{BI_PROJECT}.{BI_DATASET}.INFORMATION_SCHEMA.COLUMNS` "
        "WHERE table_name = @table_name"
    )
    job = client.query(
        sql,
        job_config=bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("table_name", "STRING", BI_TABLE)]
        ),
    )
    columns = {str(row["column_name"]).lower(): str(row["column_name"]) for row in job.result()}
    if not columns:
        fail(
            f"Table `{BI_PROJECT}.{BI_DATASET}.{BI_TABLE}` was not found, or no column is "
            "visible to this service account."
        )
    return columns


def resolve_column(
    columns: Dict[str, str], candidates: Sequence[str], required: bool = True
) -> str:
    for candidate in candidates:
        real = columns.get(candidate.lower())
        if real:
            return real
    if required:
        fail(
            f"None of the columns {list(candidates)} exist in {BI_TABLE}. "
            f"Found: {sorted(columns.values())}"
        )
    return ""


class OmsLookup:
    """One batched read of oms_active_shipments for the whole run."""

    def __init__(self) -> None:
        self.by_son: Dict[str, List[OmsRow]] = {}
        self.by_order: Dict[str, List[OmsRow]] = {}
        self.available = False

    def load(
        self,
        client: Optional["bigquery.Client"],
        shipment_order_numbers: Sequence[str],
        order_numbers: Sequence[str],
        want_json: bool,
    ) -> None:
        if client is None or not (shipment_order_numbers or order_numbers):
            return

        columns = table_columns(client)
        son_col = resolve_column(columns, SON_CANDIDATES)
        oms_col = resolve_column(columns, OMS_NUMBER_CANDIDATES)
        tenant_col = resolve_column(columns, TENANT_CANDIDATES, required=False)
        site_col = resolve_column(columns, SITE_CANDIDATES, required=False)
        order_col = resolve_column(columns, EXTERNAL_ORDER_CANDIDATES, required=False)
        status_col = resolve_column(columns, STATUS_CANDIDATES, required=False)
        json_col = resolve_column(columns, JSON_CANDIDATES, required=False) if want_json else ""

        if not order_col and order_numbers:
            warn(
                f"{BI_TABLE} has no external order number column ({list(EXTERNAL_ORDER_CANDIDATES)}); "
                "records that only carry an orderNumber will go through the Kibo fallback."
            )
        if want_json and not json_col:
            warn(
                f"{BI_TABLE} has no shipment JSON column ({list(JSON_CANDIDATES)}); "
                "the productId will be matched against the live Kibo shipment instead."
            )

        def select(column: str) -> str:
            return f"CAST(`{column}` AS STRING)" if column else "CAST(NULL AS STRING)"

        filters: List[str] = []
        params: List[Any] = []
        if shipment_order_numbers:
            filters.append(f"UPPER(TRIM(CAST(`{son_col}` AS STRING))) IN UNNEST(@sons)")
            params.append(
                bigquery.ArrayQueryParameter(
                    "sons", "STRING", [value.upper() for value in shipment_order_numbers]
                )
            )
        if order_numbers and order_col:
            filters.append(f"UPPER(TRIM(CAST(`{order_col}` AS STRING))) IN UNNEST(@orders)")
            params.append(
                bigquery.ArrayQueryParameter(
                    "orders", "STRING", [value.upper() for value in order_numbers]
                )
            )
        if not filters:
            return

        sql = f"""
            SELECT
                {select(son_col)}    AS shipment_order_number,
                {select(order_col)}  AS external_order_number,
                {select(oms_col)}    AS oms_shipment_number,
                {select(tenant_col)} AS tenant_id,
                {select(site_col)}   AS site_id,
                {select(status_col)} AS shipment_status,
                {select(json_col)}   AS shipment_json
            FROM `{BI_PROJECT}.{BI_DATASET}.{BI_TABLE}`
            WHERE {' OR '.join(filters)}
        """

        log(f"Reading {BI_TABLE} for {len(shipment_order_numbers)} shipment order number(s) "
            f"and {len(order_numbers)} order number(s).")
        job = client.query(sql, job_config=bigquery.QueryJobConfig(query_parameters=params))

        count = 0
        for row in job.result():
            oms_row = OmsRow(
                shipment_order_number=clean(row["shipment_order_number"]),
                external_order_number=clean(row["external_order_number"]),
                oms_shipment_number=clean(row["oms_shipment_number"]),
                tenant_id=clean(row["tenant_id"]),
                site_id=clean(row["site_id"]),
                shipment_status=clean(row["shipment_status"]),
                shipment_json=clean(row["shipment_json"]),
            )
            if not oms_row.oms_shipment_number:
                continue
            count += 1
            if oms_row.shipment_order_number:
                self.by_son.setdefault(oms_row.shipment_order_number.upper(), []).append(oms_row)
            if oms_row.external_order_number:
                self.by_order.setdefault(oms_row.external_order_number.upper(), []).append(oms_row)

        self.available = True
        log(f"{count} matching row(s) read from {BI_TABLE}.")

    def for_shipment_order_number(self, value: str) -> List[OmsRow]:
        return list(self.by_son.get(clean(value).upper(), []))

    def for_order_number(self, value: str) -> List[OmsRow]:
        return list(self.by_order.get(clean(value).upper(), []))


# ---------------------------------------------------------------------------
# Item matching
# ---------------------------------------------------------------------------

def item_identifiers(item: Dict[str, Any]) -> List[str]:
    """Every string on a shipment item that a CSV productId could refer to."""
    values: List[str] = []
    for key in ITEM_ID_FIELDS:
        text = clean(item.get(key))
        if text:
            values.append(text)
    options = item.get("options")
    if isinstance(options, list):
        for option in options:
            if not isinstance(option, dict):
                continue
            for key in ("stringValue", "value"):
                text = clean(option.get(key))
                if text:
                    values.append(text)
    data = item.get("data")
    if isinstance(data, dict):
        for key in ("productCode", "sku", "upc"):
            text = clean(data.get(key))
            if text:
                values.append(text)
    return values


def item_matches_product(item: Dict[str, Any], product_id: str) -> bool:
    wanted = normalize_id(product_id)
    if not wanted:
        return False
    return any(normalize_id(value) == wanted for value in item_identifiers(item))


def shipments_in_payload(payload: Any) -> List[Dict[str, Any]]:
    """Accept a shipment, a list of shipments, or a getShipments HAL response."""
    if isinstance(payload, list):
        return [entry for entry in payload if isinstance(entry, dict)]
    if not isinstance(payload, dict):
        return []
    embedded = payload.get("_embedded")
    if isinstance(embedded, dict):
        shipments = embedded.get("shipments")
        if isinstance(shipments, list):
            return [entry for entry in shipments if isinstance(entry, dict)]
    if isinstance(payload.get("items"), list) or payload.get("shipmentNumber") is not None:
        return [payload]
    return []


def parse_shipment_json(raw: str) -> List[Dict[str, Any]]:
    text = clean(raw)
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return []
    if isinstance(parsed, str):  # double encoded
        try:
            parsed = json.loads(parsed)
        except json.JSONDecodeError:
            return []
    return shipments_in_payload(parsed)


def json_carries_product(raw: str, product_id: str) -> bool:
    for shipment in parse_shipment_json(raw):
        items = shipment.get("items")
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict) and item_matches_product(item, product_id):
                    return True
    return False


def shipment_status_of(shipment: Dict[str, Any]) -> str:
    for key in ("shipmentStatus", "status"):
        value = clean(shipment.get(key))
        if value:
            return value
    workflow = shipment.get("workflowState")
    if isinstance(workflow, dict):
        return clean(workflow.get("shipmentState"))
    return ""


# ---------------------------------------------------------------------------
# Kibo client
# ---------------------------------------------------------------------------

class KiboClient:
    def __init__(self, credentials: Dict[str, str], auth_url: str, host: str, timeout: float,
                 max_retries: int) -> None:
        self.credentials = credentials
        self.auth_url = auth_url
        self.host = host
        self.timeout = timeout
        self.max_retries = max_retries
        self.session = requests.Session()
        self._tokens: Dict[str, Tuple[str, float]] = {}

    # -- authentication -----------------------------------------------------

    def token_for_tenant(self, tenant_id: str, force_refresh: bool = False) -> str:
        tenant_id = clean(tenant_id)
        cached = self._tokens.get(tenant_id)
        if cached and not force_refresh and time.time() < cached[1]:
            return cached[0]

        response = self.session.post(
            self.auth_url,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Cache-Control": "no-cache",
                "x-vol-tenant": tenant_id,
            },
            json={
                "grant_type": "client_credentials",
                "client_id": self.credentials["client_id"],
                "client_secret": self.credentials["client_secret"],
            },
            timeout=self.timeout,
        )
        if not (200 <= response.status_code < 300):
            fail(
                f"Kibo authentication failed for tenant {tenant_id} "
                f"(HTTP {response.status_code}): {excerpt_of(response)}"
            )
        try:
            data = response.json()
        except ValueError:
            fail("The Kibo authentication response was not JSON.")
            return ""

        token = clean(data.get("access_token") or data.get("accessToken") or data.get("token"))
        if not token:
            fail(f"No access token in the Kibo authentication response for tenant {tenant_id}.")

        try:
            expires_in = float(data.get("expires_in", data.get("expiresIn", 600)))
        except (TypeError, ValueError):
            expires_in = 600.0
        if expires_in > 86400:  # milliseconds
            expires_in /= 1000.0
        expires_in = max(60.0, expires_in)
        margin = min(60.0, max(5.0, expires_in * 0.10))
        self._tokens[tenant_id] = (token, time.time() + expires_in - margin)
        return token

    # -- transport ----------------------------------------------------------

    def headers(self, tenant_id: str, site_id: str, correlation_id: str = "") -> Dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.token_for_tenant(tenant_id)}",
            "x-vol-tenant": clean(tenant_id),
        }
        if clean(site_id):
            headers["x-vol-site"] = clean(site_id)
        if correlation_id:
            headers["x-vol-correlation"] = correlation_id
        return headers

    def request(
        self,
        method: str,
        url: str,
        tenant_id: str,
        site_id: str,
        json_body: Optional[Dict[str, Any]] = None,
        correlation_id: str = "",
    ) -> Tuple[Optional[Any], str]:
        last_error = ""
        last_response = None
        for attempt in range(1, self.max_retries + 1):
            try:
                response = self.session.request(
                    method,
                    url,
                    headers=self.headers(tenant_id, site_id, correlation_id),
                    json=json_body,
                    timeout=self.timeout,
                )
                last_response = response
            except requests.RequestException as exc:
                last_error = str(exc)
                if attempt < self.max_retries:
                    time.sleep(retry_sleep_seconds(attempt))
                    continue
                return None, last_error

            if response.status_code == 401 and attempt < self.max_retries:
                self.token_for_tenant(tenant_id, force_refresh=True)
                time.sleep(retry_sleep_seconds(attempt, response))
                continue
            if response.status_code in RETRYABLE_STATUS_CODES and attempt < self.max_retries:
                time.sleep(retry_sleep_seconds(attempt, response))
                continue
            return response, ""
        return last_response, last_error or "Unknown error"

    def base_url(self, tenant_id: str, shipment_number: str) -> str:
        return (
            f"https://t{clean(tenant_id)}.{self.host}"
            f"/api/commerce/shipments/{quote(clean(shipment_number), safe='')}"
        )

    # -- reads --------------------------------------------------------------

    def get_shipment(
        self, tenant_id: str, site_id: str, shipment_number: str
    ) -> Tuple[Optional[Dict[str, Any]], str]:
        response, error = self.request(
            "GET", self.base_url(tenant_id, shipment_number), tenant_id, site_id
        )
        if response is None:
            return None, error
        if not (200 <= response.status_code < 300):
            return None, f"HTTP {response.status_code}: {excerpt_of(response, 300)}"
        try:
            return response.json(), ""
        except ValueError:
            return None, "The shipment response was not JSON."

    def get_shipments_by_external_order_id(
        self, tenant_id: str, site_id: str, external_order_id: str
    ) -> Tuple[List[Dict[str, Any]], str]:
        """GET /api/commerce/shipments?filter=externalOrderId=={value}"""
        filter_value = quote(f"externalOrderId=={clean(external_order_id)}", safe="=")
        url = (
            f"https://t{clean(tenant_id)}.{self.host}"
            f"/api/commerce/shipments?filter={filter_value}&pageSize=200"
        )
        response, error = self.request("GET", url, tenant_id, site_id)
        if response is None:
            return [], error
        if not (200 <= response.status_code < 300):
            return [], f"HTTP {response.status_code}: {excerpt_of(response, 300)}"
        try:
            payload = response.json()
        except ValueError:
            return [], "The getShipments response was not JSON."
        return shipments_in_payload(payload), ""

    # -- writes -------------------------------------------------------------

    def cancel_shipment(
        self, tenant_id: str, site_id: str, shipment_number: str,
        reason_code: str, more_info: str, send_notifications: bool, correlation_id: str,
    ) -> Tuple[bool, str, str]:
        """PUT /api/commerce/shipments/{shipmentNumber}/canceled"""
        body = {
            "blockAssignment": False,
            "canceledReason": {"reasonCode": reason_code, "moreInfo": more_info},
            "sendNotifications": send_notifications,
        }
        response, error = self.request(
            "PUT", f"{self.base_url(tenant_id, shipment_number)}/canceled",
            tenant_id, site_id, json_body=body, correlation_id=correlation_id,
        )
        if response is None:
            return False, error, ""
        ok = 200 <= response.status_code < 300
        return ok, "" if ok else f"HTTP {response.status_code}", excerpt_of(response)

    def cancel_items(
        self, tenant_id: str, site_id: str, shipment_number: str,
        items: List[Dict[str, Any]], send_notifications: bool, correlation_id: str,
    ) -> Tuple[bool, str, str]:
        """
        PUT /api/commerce/shipments/{shipmentNumber}/canceledItems

        CancelItemsRequest = {blockAssignment, holdBlockAssignment, isUserAction,
                              items: [{lineId, quantity, canceledReason}], sendNotifications}
        This is the item-level endpoint; /canceled would cancel the whole shipment.
        """
        body = {
            "blockAssignment": False,
            "holdBlockAssignment": False,
            "isUserAction": True,
            "items": items,
            "sendNotifications": send_notifications,
        }
        response, error = self.request(
            "PUT", f"{self.base_url(tenant_id, shipment_number)}/canceledItems",
            tenant_id, site_id, json_body=body, correlation_id=correlation_id,
        )
        if response is None:
            return False, error, ""
        ok = 200 <= response.status_code < 300
        return ok, "" if ok else f"HTTP {response.status_code}", excerpt_of(response)


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------

@dataclass
class Result:
    source_file: str
    row_number: int
    shipment_order_number: str
    order_number: str
    product_id: str
    tenant_id: str = ""
    site_id: str = ""
    oms_shipment_number: str = ""
    line_id: str = ""
    quantity: str = ""
    action: str = ""           # cancel_shipment | cancel_items | none
    resolved_via: str = ""     # bigquery | kibo_fallback | csv | none
    outcome: str = ""          # success | failed | already_terminal | ...
    status_code: str = ""
    reason_code: str = ""
    error: str = ""
    response_excerpt: str = ""
    correlation_id: str = ""

    @property
    def succeeded(self) -> bool:
        return self.outcome == "success"

    @property
    def skipped(self) -> bool:
        return self.outcome in SKIP_OUTCOMES


@dataclass
class Target:
    """One shipment to act on, with the items when the action is item-level."""
    tenant_id: str
    site_id: str
    oms_shipment_number: str
    resolved_via: str
    shipment: Optional[Dict[str, Any]] = None
    notes: str = ""


@dataclass
class FileOutcome:
    path: Path
    results: List[Result] = field(default_factory=list)
    read_error: str = ""

    @property
    def has_failure(self) -> bool:
        return bool(self.read_error) or any(
            not (result.succeeded or result.skipped) for result in self.results
        )


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

class Resolver:
    def __init__(
        self,
        lookup: OmsLookup,
        kibo: KiboClient,
        tenant_map: Dict[str, str],
        default_tenant_id: str,
        default_site_id: str,
        derive_external: bool,
    ) -> None:
        self.lookup = lookup
        self.kibo = kibo
        self.tenant_map = tenant_map
        self.default_tenant_id = default_tenant_id
        self.default_site_id = default_site_id
        self.derive_external = derive_external

    def tenant_for(self, record: InputRecord, from_row: str = "") -> Tuple[str, str]:
        """
        The tenant to use for this record, as (tenant_id, error).

        Order: the CSV's own tenantId, then whatever oms_active_shipments stored,
        then the brand prefix of the shipmentOrderNumber or orderNumber, then
        --default-tenant-id. Nothing is guessed: an unknown prefix with no
        default is an error, because the wrong tenant would cancel the wrong
        brand's shipment.
        """
        explicit = clean(record.tenant_id) or clean(from_row)
        if explicit:
            return explicit, ""

        prefix = brand_prefix(record.shipment_order_number, record.order_number)
        mapped = self.tenant_map.get(prefix, "")
        if mapped:
            return mapped, ""

        if self.default_tenant_id:
            return self.default_tenant_id, ""

        if not prefix:
            return "", (
                "no tenantId: neither the CSV nor oms_active_shipments supplied one, and "
                "the number does not start with a two-letter brand code"
            )
        return "", (
            f"no tenantId for brand prefix '{prefix}': add it to the tenant map "
            "(KIBO_TENANT_MAP or --tenant-map), or set a tenantId column on the row"
        )

    def targets_for(self, record: InputRecord) -> Tuple[List[Target], str]:
        """Return (targets, error). An empty list with no error means 'not found'."""
        rows: List[OmsRow] = []
        if record.shipment_order_number:
            rows = self.lookup.for_shipment_order_number(record.shipment_order_number)
        elif record.order_number:
            rows = self.lookup.for_order_number(record.order_number)

        if rows:
            if record.product_id:
                narrowed = [row for row in rows
                            if json_carries_product(row.shipment_json, record.product_id)]
                # When no shipment_json is stored, keep every row and let the live Kibo
                # read decide which shipment carries the product.
                if narrowed:
                    rows = narrowed
            targets: List[Target] = []
            for row in rows:
                tenant_id, error = self.tenant_for(record, row.tenant_id)
                if error:
                    return [], error
                targets.append(
                    Target(
                        tenant_id=tenant_id,
                        site_id=record.site_id or row.site_id or self.default_site_id,
                        oms_shipment_number=row.oms_shipment_number,
                        resolved_via="bigquery",
                    )
                )
            return targets, ""

        return self._fallback(record)

    def _fallback(self, record: InputRecord) -> Tuple[List[Target], str]:
        external_order_id = record.order_number
        if not external_order_id and self.derive_external and record.shipment_order_number:
            external_order_id = derive_external_order_id(record.shipment_order_number)
        if not external_order_id:
            return [], "not found in oms_active_shipments and no orderNumber to fall back on"

        tenant_id, tenant_error = self.tenant_for(record)
        if tenant_error:
            return [], f"not found in oms_active_shipments, and {tenant_error}"
        site_id = record.site_id or self.default_site_id
        shipments, error = self.kibo.get_shipments_by_external_order_id(
            tenant_id, site_id, external_order_id
        )
        if error:
            return [], f"getShipments by externalOrderId '{external_order_id}' failed: {error}"
        if not shipments:
            return [], f"no shipment in Kibo for externalOrderId '{external_order_id}'"

        targets: List[Target] = []
        for shipment in shipments:
            number = clean(shipment.get("shipmentNumber"))
            if not number:
                continue
            if record.shipment_order_number:
                stored = clean((shipment.get("data") or {}).get("shipmentOrderNumber"))
                if stored and normalize_id(stored) != normalize_id(record.shipment_order_number):
                    continue
            if record.product_id:
                items = shipment.get("items")
                if isinstance(items, list) and not any(
                    isinstance(item, dict) and item_matches_product(item, record.product_id)
                    for item in items
                ):
                    continue
            targets.append(
                Target(
                    tenant_id=record.tenant_id or clean(shipment.get("tenantId")) or tenant_id,
                    site_id=record.site_id or clean(shipment.get("siteId")) or site_id,
                    oms_shipment_number=number,
                    resolved_via="kibo_fallback",
                    shipment=shipment,
                )
            )

        if not targets:
            return [], (
                f"getShipments for externalOrderId '{external_order_id}' returned "
                f"{len(shipments)} shipment(s), none matching the record"
            )
        return targets, ""


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

class Processor:
    def __init__(self, kibo: KiboClient, resolver: Resolver, args: argparse.Namespace) -> None:
        self.kibo = kibo
        self.resolver = resolver
        self.args = args

    def process_record(self, record: InputRecord) -> List[Result]:
        base = Result(
            source_file=record.source_file,
            row_number=record.row_number,
            shipment_order_number=record.shipment_order_number,
            order_number=record.order_number,
            product_id=record.product_id,
        )

        if not (record.shipment_order_number or record.order_number):
            base.outcome = "failed"
            base.resolved_via = "none"
            base.error = "the row carries neither a shipmentOrderNumber nor an orderNumber"
            return [base]

        try:
            targets, error = self.resolver.targets_for(record)
        except Exception as exc:  # noqa: BLE001  one bad record must not stop the run
            base.outcome = "failed"
            base.error = f"resolution raised {type(exc).__name__}: {exc}"
            return [base]

        if error or not targets:
            base.outcome = "failed"
            base.resolved_via = "none"
            base.error = error or "no shipment matched this record"
            return [base]

        results: List[Result] = []
        for target in targets:
            if record.product_id:
                results.append(self._cancel_item(record, target, base))
            else:
                results.append(self._cancel_shipment(record, target, base))
            if self.args.sleep_between_requests > 0:
                time.sleep(self.args.sleep_between_requests)
        return results

    # -- helpers ------------------------------------------------------------

    def _result_for(self, record: InputRecord, target: Target, base: Result) -> Result:
        return Result(
            source_file=base.source_file,
            row_number=base.row_number,
            shipment_order_number=base.shipment_order_number,
            order_number=base.order_number,
            product_id=base.product_id,
            tenant_id=target.tenant_id,
            site_id=target.site_id,
            oms_shipment_number=target.oms_shipment_number,
            resolved_via=target.resolved_via,
            reason_code=record.reason_code or self.args.reason_code,
            correlation_id=uuid.uuid4().hex,
        )

    def _load_shipment(self, target: Target) -> Tuple[Optional[Dict[str, Any]], str]:
        if target.shipment is not None:
            return target.shipment, ""
        return self.kibo.get_shipment(
            target.tenant_id, target.site_id, target.oms_shipment_number
        )

    # -- actions ------------------------------------------------------------

    def _cancel_shipment(self, record: InputRecord, target: Target, base: Result) -> Result:
        result = self._result_for(record, target, base)
        result.action = "cancel_shipment"

        shipment, error = self._load_shipment(target)
        if error:
            result.outcome = "failed"
            result.error = f"could not read the shipment: {error}"
            return result

        status = shipment_status_of(shipment or {})
        if is_terminal_status(status):
            result.outcome = "already_terminal"
            result.error = f"the shipment is already {status}"
            return result

        if self.args.dry_run:
            result.outcome = "dry_run"
            result.error = f"would cancel the whole shipment (current status {status or 'unknown'})"
            return result

        ok, error, excerpt = self.kibo.cancel_shipment(
            target.tenant_id, target.site_id, target.oms_shipment_number,
            record.reason_code or self.args.reason_code,
            record.more_info or self.args.more_info,
            self.args.send_notifications,
            result.correlation_id,
        )
        result.outcome = "success" if ok else "failed"
        result.error = error
        result.status_code = error.replace("HTTP ", "") if error.startswith("HTTP ") else ""
        result.response_excerpt = excerpt
        return result

    def _cancel_item(self, record: InputRecord, target: Target, base: Result) -> Result:
        result = self._result_for(record, target, base)
        result.action = "cancel_items"

        shipment, error = self._load_shipment(target)
        if error:
            result.outcome = "failed"
            result.error = f"could not read the shipment: {error}"
            return result
        shipment = shipment or {}

        status = shipment_status_of(shipment)
        if is_terminal_status(status):
            result.outcome = "already_terminal"
            result.error = f"the shipment is already {status}"
            return result

        items = shipment.get("items")
        matches = [
            item for item in (items if isinstance(items, list) else [])
            if isinstance(item, dict) and item_matches_product(item, record.product_id)
        ]

        if not matches:
            canceled = shipment.get("canceledItems")
            already = [
                item for item in (canceled if isinstance(canceled, list) else [])
                if isinstance(item, dict) and item_matches_product(item, record.product_id)
            ]
            if already:
                result.outcome = "item_already_canceled"
                result.error = f"product {record.product_id} is already in canceledItems"
            else:
                result.outcome = "failed"
                result.error = (
                    f"product {record.product_id} is not among the active items of "
                    f"shipment {target.oms_shipment_number}"
                )
            return result

        reason = {
            "reasonCode": record.reason_code or self.args.reason_code,
            "moreInfo": record.more_info or self.args.more_info,
        }
        payload_items: List[Dict[str, Any]] = []
        line_ids: List[str] = []
        quantities: List[str] = []
        wanted_quantity = int(record.quantity) if record.quantity.isdigit() else 0

        for item in matches:
            line_id = item.get("lineId")
            if line_id is None:
                result.outcome = "failed"
                result.error = f"the matching item on shipment {target.oms_shipment_number} has no lineId"
                return result
            available = item.get("quantity")
            try:
                available_int = int(available)
            except (TypeError, ValueError):
                available_int = 0
            quantity = wanted_quantity if 0 < wanted_quantity <= available_int else available_int
            if quantity <= 0:
                result.outcome = "failed"
                result.error = f"line {line_id} has no cancellable quantity (quantity={available})"
                return result
            payload_items.append(
                {"lineId": int(line_id), "quantity": int(quantity), "canceledReason": reason}
            )
            line_ids.append(str(line_id))
            quantities.append(str(quantity))

        result.line_id = ",".join(line_ids)
        result.quantity = ",".join(quantities)

        if self.args.dry_run:
            result.outcome = "dry_run"
            result.error = (
                f"would cancel line(s) {result.line_id} quantity {result.quantity} "
                f"on shipment {target.oms_shipment_number}"
            )
            return result

        ok, error, excerpt = self.kibo.cancel_items(
            target.tenant_id, target.site_id, target.oms_shipment_number,
            payload_items, self.args.send_notifications, result.correlation_id,
        )
        result.outcome = "success" if ok else "failed"
        result.error = error
        result.status_code = error.replace("HTTP ", "") if error.startswith("HTTP ") else ""
        result.response_excerpt = excerpt
        return result


# ---------------------------------------------------------------------------
# Logging and file moves
# ---------------------------------------------------------------------------

RESULT_FIELDS = [
    "source_file", "row_number", "shipment_order_number", "order_number", "product_id",
    "tenant_id", "site_id", "oms_shipment_number", "line_id", "quantity",
    "action", "resolved_via", "outcome", "status_code", "reason_code",
    "error", "response_excerpt", "correlation_id",
]


def write_log(path: Path, outcomes: List[FileOutcome], args: argparse.Namespace,
              started_at: datetime) -> Tuple[int, int, int]:
    succeeded = sum(1 for outcome in outcomes for r in outcome.results if r.succeeded)
    skipped = sum(1 for outcome in outcomes for r in outcome.results if r.skipped)
    failed = sum(
        1 for outcome in outcomes for r in outcome.results if not (r.succeeded or r.skipped)
    )

    lines: List[str] = []
    lines.append(f"Run started   : {started_at.isoformat(timespec='seconds')}")
    lines.append(f"Run finished  : {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    lines.append(f"Mode          : {'DRY RUN - nothing was sent to Kibo' if args.dry_run else 'LIVE'}")
    lines.append(f"Kibo host     : {args.host}")
    lines.append(f"Files read    : {len(outcomes)}")
    lines.append(f"Records       : {succeeded + skipped + failed} "
                 f"({succeeded} succeeded, {skipped} skipped, {failed} failed)")
    lines.append("")

    for outcome in outcomes:
        lines.append("=" * 78)
        lines.append(f"FILE {outcome.path.name}")
        if outcome.read_error:
            lines.append(f"  UNREADABLE: {outcome.read_error}")
            lines.append("")
            continue
        lines.append(f"  records: {len(outcome.results)}")
        lines.append("")

        ok_rows = [r for r in outcome.results if r.succeeded]
        skip_rows = [r for r in outcome.results if r.skipped]
        bad_rows = [r for r in outcome.results if not (r.succeeded or r.skipped)]

        def render(rows: List[Result], title: str) -> None:
            lines.append(f"  {title} ({len(rows)})")
            if not rows:
                lines.append("    -")
                return
            for row in rows:
                identity = (
                    f"row {row.row_number}: "
                    f"shipmentOrderNumber={row.shipment_order_number or '-'} "
                    f"orderNumber={row.order_number or '-'} "
                    f"productId={row.product_id or '-'}"
                )
                detail = (
                    f"      -> {row.action or 'none'} "
                    f"shipment={row.oms_shipment_number or '-'} "
                    f"tenant={row.tenant_id or '-'} "
                    f"line={row.line_id or '-'} qty={row.quantity or '-'} "
                    f"via={row.resolved_via or '-'} outcome={row.outcome}"
                )
                lines.append(f"    {identity}")
                lines.append(detail)
                if row.error:
                    lines.append(f"         note: {row.error}")
                if row.response_excerpt and not row.succeeded:
                    lines.append(f"         response: {row.response_excerpt[:400]}")

        render(ok_rows, "SUCCEEDED")
        lines.append("")
        render(skip_rows, "SKIPPED (nothing to do)")
        lines.append("")
        render(bad_rows, "FAILED")
        lines.append("")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return succeeded, skipped, failed


def write_results_csv(path: Path, outcomes: List[FileOutcome]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=RESULT_FIELDS)
        writer.writeheader()
        for outcome in outcomes:
            if outcome.read_error and not outcome.results:
                writer.writerow(
                    {
                        "source_file": outcome.path.name,
                        "row_number": "",
                        "outcome": "failed",
                        "error": outcome.read_error,
                        **{key: "" for key in RESULT_FIELDS
                           if key not in {"source_file", "row_number", "outcome", "error"}},
                    }
                )
                continue
            for result in outcome.results:
                row = asdict(result)
                writer.writerow({key: row.get(key, "") for key in RESULT_FIELDS})


def move_file(path: Path, destination_dir: Path, stamp: str) -> Path:
    destination_dir.mkdir(parents=True, exist_ok=True)
    target = destination_dir / f"{stamp}__{path.name}"
    counter = 1
    while target.exists():
        target = destination_dir / f"{stamp}__{counter}__{path.name}"
        counter += 1
    shutil.move(str(path), str(target))
    return target


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Apply the cancellation CSV files in input/ to Kibo.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--input-dir", type=Path, default=Path("input"))
    parser.add_argument("--archive-dir", type=Path, default=Path("archive"))
    parser.add_argument("--error-dir", type=Path, default=Path("error"))
    parser.add_argument("--log-dir", type=Path, default=Path("logs"))
    parser.add_argument("--host", default=os.environ.get("KIBO_HOST") or DEFAULT_HOST)
    parser.add_argument("--auth-url", default=DEFAULT_AUTH_URL)
    parser.add_argument("--tenant-map", default="",
                        help='JSON object merged over the built-in brand map, '
                             'e.g. \'{"XY": "40001"}\'.')
    parser.add_argument("--default-tenant-id",
                        default=os.environ.get("KIBO_TENANT_ID") or DEFAULT_TENANT_ID,
                        help="Last resort when the brand prefix is not in the tenant map. "
                             "Empty by default: an unknown prefix fails the record rather "
                             "than cancelling against the wrong brand.")
    parser.add_argument("--default-site-id",
                        default=os.environ.get("KIBO_SITE_ID") or DEFAULT_SITE_ID,
                        help="Fallback x-vol-site. Empty by default; the header is then "
                             "omitted unless BigQuery or Kibo supplies a site.")
    parser.add_argument("--reason-code", default=DEFAULT_REASON_CODE)
    parser.add_argument("--more-info", default=DEFAULT_MORE_INFO)
    parser.add_argument("--dry-run", action="store_true",
                        help="Resolve and report, but send no cancellation to Kibo.")
    parser.add_argument("--no-send-notifications", dest="send_notifications",
                        action="store_false", help="Set sendNotifications to false.")
    parser.set_defaults(send_notifications=True)
    parser.add_argument("--no-derive-external-order-id", dest="derive_external_order_id",
                        action="store_false",
                        help="Do not derive the externalOrderId from the shipmentOrderNumber "
                             "when falling back to getShipments.")
    parser.set_defaults(derive_external_order_id=True)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--max-retries", type=int, default=3)
    parser.add_argument("--sleep-between-requests", type=float, default=0.2)
    parser.add_argument("--limit", type=int, default=0,
                        help="Maximum number of records to process, 0 for no limit.")

    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be greater than 0.")
    if args.max_retries < 1:
        parser.error("--max-retries must be at least 1.")
    args.host = re.sub(r"^https?://", "", args.host.strip()).split("/", 1)[0]
    args.host = re.sub(r"^t\d+(?:-s\d+)?\.", "", args.host, flags=re.IGNORECASE).rstrip(".")
    if not args.host:
        parser.error("--host cannot be empty.")
    return args


def main() -> int:
    args = parse_args()
    started_at = datetime.now(timezone.utc)
    stamp = started_at.strftime("%Y%m%d-%H%M%S")

    input_files = sorted(
        path for path in args.input_dir.glob("*")
        if path.is_file() and path.suffix.lower() in {".csv", ".txt"}
    )
    if not input_files:
        log(f"No CSV file in {args.input_dir}. Nothing to do.")
        return 0

    log(f"Found {len(input_files)} file(s) in {args.input_dir}: "
        + ", ".join(path.name for path in input_files))
    if args.dry_run:
        log("DRY RUN: no cancellation will be sent to Kibo.")

    # 1. Read every file.
    outcomes: List[FileOutcome] = []
    records: List[Tuple[FileOutcome, InputRecord]] = []
    for path in input_files:
        parsed, read_error = read_csv_records(path)
        outcome = FileOutcome(path=path, read_error=read_error)
        outcomes.append(outcome)
        if read_error:
            warn(f"{path.name}: {read_error}")
            continue
        for record in parsed:
            records.append((outcome, record))

    if args.limit > 0 and len(records) > args.limit:
        log(f"Limiting the run to the first {args.limit} of {len(records)} record(s).")
        records = records[: args.limit]

    if records:
        # 2. One BigQuery read for the whole run.
        shipment_order_numbers = sorted({
            record.shipment_order_number for _, record in records if record.shipment_order_number
        })
        order_numbers = sorted({
            record.order_number for _, record in records
            if record.order_number and not record.shipment_order_number
        })
        want_json = any(record.product_id for _, record in records)

        lookup = OmsLookup()
        try:
            lookup.load(bigquery_client(), shipment_order_numbers, order_numbers, want_json)
        except SystemExit:
            raise
        except Exception as exc:  # noqa: BLE001  BigQuery down must not stop the fallback
            warn(f"The BigQuery lookup failed ({type(exc).__name__}: {exc}); "
                 "every record will go through the Kibo fallback.")

        # 3. Apply.
        kibo = KiboClient(
            credentials=kibo_credentials(),
            auth_url=args.auth_url,
            host=args.host,
            timeout=args.timeout,
            max_retries=args.max_retries,
        )
        resolver = Resolver(
            lookup=lookup,
            kibo=kibo,
            tenant_map=load_tenant_map(args.tenant_map),
            default_tenant_id=args.default_tenant_id,
            default_site_id=args.default_site_id,
            derive_external=args.derive_external_order_id,
        )
        processor = Processor(kibo=kibo, resolver=resolver, args=args)

        for index, (outcome, record) in enumerate(records, start=1):
            log(f"[{index}/{len(records)}] {record.source_file} row {record.row_number}: {record.label}")
            for result in processor.process_record(record):
                outcome.results.append(result)
                log(f"    {result.outcome.upper()} "
                    f"action={result.action or '-'} "
                    f"shipment={result.oms_shipment_number or '-'} "
                    f"via={result.resolved_via or '-'}"
                    + (f" :: {result.error}" if result.error else ""))

    # 4. Logs.
    log_path = args.log_dir / f"run-{stamp}.log"
    csv_path = args.log_dir / f"run-{stamp}.csv"
    succeeded, skipped, failed = write_log(log_path, outcomes, args, started_at)
    write_results_csv(csv_path, outcomes)

    # 5. Move the inputs.
    for outcome in outcomes:
        destination = args.error_dir if outcome.has_failure else args.archive_dir
        if args.dry_run:
            log(f"DRY RUN: {outcome.path.name} would move to {destination}/")
            continue
        moved = move_file(outcome.path, destination, stamp)
        log(f"{outcome.path.name} -> {moved}")

    log("")
    log(f"Summary: {succeeded} succeeded, {skipped} skipped, {failed} failed.")
    log(f"Log: {log_path}")
    log(f"Results CSV: {csv_path}")

    # A failed record is reported in the log and the file is moved to error/, but the
    # job itself stays green so that one bad record does not look like a broken workflow.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
