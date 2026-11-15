#!/usr/bin/env python3
"""
Consume the warehouse-cancellation CSV files dropped in warehouse/input/ and send a
CANCEL event to the GXO shipmentStatusUpdate endpoint for each shipment.

This is the warehouse counterpart of process_cancellations.py. That one cancels
in Kibo; this one tells the warehouse to stop a shipment it already has.

CSV columns (header matching is case / space / underscore insensitive):

    shipmentOrderNumber   e.g. OW-USA00037769          required
    productId             productCode / UPC / SKU      optional
    orderNumber           e.g. OW-US-00037769          optional, see below
    tenantId / siteId     override the defaults        optional
    reasonMessage         overrides --reason-message   optional

What happens per row
--------------------
1. shipmentOrderNumber, no productId
       The whole shipment is cancelled at the warehouse. No Kibo call.

2. shipmentOrderNumber + productId
       The row is a request to cancel that product, and the warehouse event has
       no item granularity: it stops a whole shipment. So the shipment is read
       from Kibo first and the product decides which of two things happens.

           GET /api/commerce/shipments?filter=externalOrderId=={orderNumber}

       The shipments whose data.shipmentOrderNumber matches the row are kept
       (when none of them carries that field, every shipment of the order is
       kept). Then:

         - every item on those shipments is the given product
               -> the warehouse cancellation is sent, as in case 1.

         - the shipments carry other products too
               -> the warehouse is NOT told anything. Only that product is
                  cancelled in Kibo, with
                      PUT /api/commerce/shipments/{shipmentNumber}/canceledItems
                  so the rest of the shipment goes out as planned. The outcome
                  is 'item_canceled_in_kibo'.

       The externalOrderId comes from the orderNumber column when there is
       one, otherwise it is derived from the shipmentOrderNumber by replacing
       the 6th character with a hyphen ("OW-USA00037769" -> "OW-US-00037769").
       Turn the derivation off with --no-derive-external-order-id.

3. Then, for every record whose whole shipment is to be cancelled
       `tlg-wlfs-prd`.til.prd_orders is read, joined to til.prd_brandConfig,
       for the logisticOrderNumber, locationName, brand and brandCode of the
       shipment, keeping only locations whose name carries 'GX'. One CANCEL
       event is POSTed per matching row:

           POST https://prdedi.gxo.com:4084/sync/mft/tlg_it/shipmentStatusUpdate

       The event always carries an EMPTY products array: the warehouse reads
       that as "the whole shipment", which is the only thing it can act on.

Tenant resolution (only needed for rows that carry a productId)
---------------------------------------------------------------
    1. a tenantId column on the CSV row
    2. the two-letter brand prefix of the number, through BRAND_TENANTS
    3. --default-tenant-id, which is empty by default

Credentials come from environment variables (GitHub repository secrets):
    WLFS_BIGQUERY_CREDS   service account JSON for tlg-wlfs-prd
    GXO_API_KEY           the endpoint key, raw or as {"api_key": "..."}
    KIBO_API_CREDS        {"client_id": "...", "client_secret": "..."} —
                          only read when a row carries a productId

Outputs (all under warehouse/, so the Kibo worker's folders stay untouched)
--------------------------------------------------------------------------
    warehouse/logs/run-<stamp>.log          human readable, one line per record
    warehouse/logs/run-<stamp>.csv          the same records, structured
    warehouse/archive/<stamp>__<name>.csv   every record succeeded or was skipped
    warehouse/error/<stamp>__<name>.csv     the file was unreadable, or a record failed
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
import ssl
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import requests
import urllib3
from requests.adapters import HTTPAdapter
from urllib3.util.ssl_ import create_urllib3_context

try:
    from google.cloud import bigquery
    from google.oauth2 import service_account
except ImportError:  # the worker cannot do anything useful without it, but say so clearly
    bigquery = None  # type: ignore[assignment]
    service_account = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

WLFS_PROJECT = "tlg-wlfs-prd"
WLFS_DATASET = "til"
ORDERS_TABLE = "prd_orders"
BRAND_CONFIG_TABLE = "prd_brandConfig"

DEFAULT_GXO_URL = "https://prdedi.gxo.com:4084/sync/mft/tlg_it/shipmentStatusUpdate"

DEFAULT_KIBO_HOST = "tp3.mozu.com"
DEFAULT_KIBO_AUTH_URL = "https://home.mozu.com/api/platform/applications/authtickets/oauth"

DEFAULT_UPDATE_REASON = "external"
# Used when the gate sends the cancellation to Kibo instead of the warehouse.
DEFAULT_REASON_CODE = "CustomerChangedTheirMind"
DEFAULT_REASON_MESSAGE = "Kibo notification cancellation"

DEFAULT_TENANT_ID = ""
DEFAULT_SITE_ID = ""

# Kept in step with BRAND_TENANTS in scripts/process_cancellations.py and in
# shared/console.py of the applications repository.
BRAND_TENANTS = {
    "DG": "29916", "LN": "29917", "MO": "29988", "SC": "29989",
    "CO": "29990", "RC": "29990", "WL": "29991", "SW": "29992",
    "CA": "29993", "CV": "30014", "HB": "30014", "BA": "30014",
    "RL": "30016", "OW": "30016", "FA": "30017", "VW": "30017",
    "DJ": "30018", "AT": "30019", "LA": "30019", "PL": "30020",
    "CL": "30020", "HE": "30021", "FE": "30022", "CR": "30022",
    "FO": "30023", "EF": "30023", "PJ": "30475", "FU": "32145",
    "WF": "32147", "ZH": "32147", "VE": "32147", "BO": "33692",
    "AL": "33693", "MA": "33693", "PA": "33693", "FL": "33693",
    "BB": "33694", "TM": "54516",
}

BQ_SCOPES = ["https://www.googleapis.com/auth/bigquery"]
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

# Every string on a shipment item a CSV productId could be referring to.
ITEM_ID_FIELDS = (
    "productCode", "upc", "sku", "variationProductCode", "partNumber",
    "originalOrderItemId", "productId", "itemId",
)

# Outcomes that count as the record having been dealt with.
SUCCESS_OUTCOMES = {"success", "item_canceled_in_kibo"}
# Outcomes that are neither a success nor a failure: nothing needed doing.
SKIP_OUTCOMES = {"item_already_canceled", "already_terminal", "dry_run"}

TERMINAL_STATUSES = {
    "CANCELED", "CANCELLED", "FULFILLED", "SHIPPED", "COMPLETED", "COMPLETE",
    "DELIVERED", "CLOSED",
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


def stamp_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def event_timestamp() -> str:
    """'2026-11-15T09:33:21.482Z', the format the GXO endpoint expects."""
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


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
            fail(f'{source} must be a JSON object of {{"BRAND": "tenantId"}}.')
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
    raw = os.environ.get(name, "").strip().lstrip("\ufeff")
    if not raw:
        fail(f"Environment variable {name} is empty. Check the repository secret.")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        fail(f"Environment variable {name} does not contain valid JSON: {exc}")
    if not isinstance(data, dict):
        fail(f"Environment variable {name} must contain a JSON object.")
    return data


def gxo_api_key() -> str:
    """
    The endpoint key. GXO_API_KEY may hold the raw key or the whole
    GXO_endpoint_key.json, which carries it under 'api_key'.
    """
    raw = os.environ.get("GXO_API_KEY", "").strip().lstrip("\ufeff")
    if not raw:
        fail("Environment variable GXO_API_KEY is empty. Check the repository secret.")
    if raw.startswith("{"):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            fail(f"GXO_API_KEY looks like JSON but does not parse: {exc}")
            return ""
        key = clean(data.get("api_key") or data.get("apiKey") or data.get("key"))
        if not key:
            fail("GXO_API_KEY is a JSON object with no 'api_key' field.")
        return key
    return raw


def kibo_credentials() -> Dict[str, str]:
    data = load_json_env("KIBO_API_CREDS")
    client_id = clean(data.get("client_id"))
    client_secret = clean(data.get("client_secret"))
    if not client_id or not client_secret:
        fail("KIBO_API_CREDS must carry a non-empty client_id and client_secret.")
    return {"client_id": client_id, "client_secret": client_secret}


def bigquery_client() -> "bigquery.Client":
    if bigquery is None:
        fail("google-cloud-bigquery is not installed; run pip install -r requirements.txt.")
    info = load_json_env("WLFS_BIGQUERY_CREDS")
    for key in ("type", "project_id", "private_key", "client_email"):
        if not info.get(key):
            fail(f"WLFS_BIGQUERY_CREDS is missing the '{key}' field.")
    project = clean(info.get("project_id")) or WLFS_PROJECT
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
    tenant_id: str = ""
    site_id: str = ""
    quantity: str = ""
    reason_code: str = ""
    reason_message: str = ""

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
    "tenantid": "tenant_id",
    "siteid": "site_id",
    "reasonmessage": "reason_message",
    "moreinfo": "reason_message",
    "reasoncode": "reason_code",
    "reason": "reason_code",
    "quantity": "quantity",
    "qty": "quantity",
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

    if "shipment_order_number" not in set(mapping.values()):
        return [], (
            "no shipmentOrderNumber column. The warehouse cancellation is keyed on it, "
            "so every row needs one. Found: " + ", ".join(str(name) for name in header)
        )

    records: List[InputRecord] = []
    for row_number, row in enumerate(reader, start=2):
        if not row or all(not clean(cell) for cell in row):
            continue
        record = InputRecord(source_file=path.name, row_number=row_number)
        for index, field_name in mapping.items():
            if index < len(row):
                setattr(record, field_name, clean(row[index]))
        records.append(record)

    if not records:
        return [], "the file has a header but no data rows"
    return records, ""


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


def is_terminal_status(status: str) -> bool:
    return status.upper().replace(" ", "").replace("-", "_") in TERMINAL_STATUSES


def shipment_status_of(shipment: Dict[str, Any]) -> str:
    for key in ("shipmentStatus", "status"):
        value = clean(shipment.get(key))
        if value:
            return value
    workflow = shipment.get("workflowState")
    if isinstance(workflow, dict):
        return clean(workflow.get("shipmentState"))
    return ""


def item_matches_product(item: Dict[str, Any], product_id: str) -> bool:
    wanted = normalize_id(product_id)
    if not wanted:
        return False
    return any(normalize_id(value) == wanted for value in item_identifiers(item))


def item_label(item: Dict[str, Any]) -> str:
    """A short name for an item, for the 'other products' message."""
    for key in ("productCode", "sku", "upc", "variationProductCode", "partNumber"):
        text = clean(item.get(key))
        if text:
            return text
    name = clean(item.get("name"))
    return name or "(unnamed item)"


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


def shipment_order_number_of(shipment: Dict[str, Any]) -> str:
    data = shipment.get("data")
    if isinstance(data, dict):
        value = clean(data.get("shipmentOrderNumber"))
        if value:
            return value
    return clean(shipment.get("shipmentOrderNumber"))


def shipment_items(shipment: Dict[str, Any]) -> List[Dict[str, Any]]:
    items = shipment.get("items")
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


# ---------------------------------------------------------------------------
# Kibo client — reads only; this worker never writes to Kibo
# ---------------------------------------------------------------------------

class KiboClient:
    def __init__(self, credentials: Dict[str, str], auth_url: str, host: str,
                 timeout: float, max_retries: int) -> None:
        self.credentials = credentials
        self.auth_url = auth_url
        self.host = host
        self.timeout = timeout
        self.max_retries = max_retries
        self.session = requests.Session()
        self._tokens: Dict[str, Tuple[str, float]] = {}

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

    def headers(self, tenant_id: str, site_id: str,
                correlation_id: str = "") -> Dict[str, str]:
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

    def request(self, method: str, url: str, tenant_id: str, site_id: str,
                json_body: Optional[Dict[str, Any]] = None,
                correlation_id: str = "") -> Tuple[Optional[Any], str]:
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

    def cancel_items(self, tenant_id: str, site_id: str, shipment_number: str,
                     items: List[Dict[str, Any]], send_notifications: bool,
                     correlation_id: str) -> Tuple[bool, str, str]:
        """
        PUT /api/commerce/shipments/{shipmentNumber}/canceledItems

        CancelItemsRequest = {blockAssignment, holdBlockAssignment, isUserAction,
                              items: [{lineId, quantity, canceledReason}],
                              sendNotifications}
        This is the item-level endpoint; /canceled would cancel the whole
        shipment, which is precisely what this worker is avoiding here.
        """
        url = (
            f"https://t{clean(tenant_id)}.{self.host}"
            f"/api/commerce/shipments/{quote(clean(shipment_number), safe='')}/canceledItems"
        )
        body = {
            "blockAssignment": False,
            "holdBlockAssignment": False,
            "isUserAction": True,
            "items": items,
            "sendNotifications": send_notifications,
        }
        response, error = self.request(
            "PUT", url, tenant_id, site_id, json_body=body, correlation_id=correlation_id
        )
        if response is None:
            return False, error, ""
        ok = 200 <= response.status_code < 300
        return ok, "" if ok else f"HTTP {response.status_code}", excerpt_of(response)


# ---------------------------------------------------------------------------
# The product gate
# ---------------------------------------------------------------------------

@dataclass
class GateVerdict:
    """
    What to do with a row that names a product.

    decision is one of:
        'warehouse'  the product is the whole shipment -> send the CANCEL event
        'kibo_item'  the shipment holds other products -> cancel only that item
                     in Kibo and tell the warehouse nothing
        'fail'       the row could not be judged; detail says why
    """
    decision: str
    detail: str = ""
    tenant_id: str = ""
    site_id: str = ""
    shipments: List[Dict[str, Any]] = field(default_factory=list)
    items_seen: int = 0


class ProductGate:
    """
    Reads the shipment from Kibo and decides which of the two cancellations a
    product-bearing row gets.

    The warehouse event has no item granularity: it stops a whole shipment. So
    a shipment carrying other products is never sent to the warehouse — the one
    product is cancelled in Kibo instead, and the rest ships as planned.
    """

    def __init__(self, kibo: KiboClient, tenant_map: Dict[str, str],
                 default_tenant_id: str, default_site_id: str,
                 derive_external: bool) -> None:
        self.kibo = kibo
        self.tenant_map = tenant_map
        self.default_tenant_id = default_tenant_id
        self.default_site_id = default_site_id
        self.derive_external = derive_external

    def tenant_for(self, record: InputRecord) -> Tuple[str, str]:
        explicit = clean(record.tenant_id)
        if explicit:
            return explicit, ""
        prefix = brand_prefix(record.shipment_order_number, record.order_number)
        mapped = self.tenant_map.get(prefix, "")
        if mapped:
            return mapped, ""
        if self.default_tenant_id:
            return self.default_tenant_id, ""
        if not prefix:
            return "", ("no tenantId: the number does not start with a two-letter brand code "
                        "and the row carries no tenantId")
        return "", (f"no tenantId for brand prefix '{prefix}': add it to the tenant map "
                    "(KIBO_TENANT_MAP or --tenant-map), or set a tenantId column on the row")

    def check(self, record: InputRecord) -> GateVerdict:
        external_order_id = clean(record.order_number)
        if not external_order_id and self.derive_external:
            external_order_id = derive_external_order_id(record.shipment_order_number)
        if not external_order_id:
            return GateVerdict(
                "fail",
                "a productId was given but there is no orderNumber to read the shipment with, "
                "and the externalOrderId could not be derived from the shipmentOrderNumber",
            )

        tenant_id, tenant_error = self.tenant_for(record)
        if tenant_error:
            return GateVerdict("fail", tenant_error)

        site_id = clean(record.site_id) or self.default_site_id
        shipments, error = self.kibo.get_shipments_by_external_order_id(
            tenant_id, site_id, external_order_id
        )
        if error:
            return GateVerdict(
                "fail",
                f"getShipments by externalOrderId '{external_order_id}' failed: {error}",
                tenant_id, site_id,
            )
        if not shipments:
            return GateVerdict(
                "fail",
                f"no shipment in Kibo for externalOrderId '{external_order_id}'",
                tenant_id, site_id,
            )

        # Keep the shipments that belong to this shipmentOrderNumber. When no
        # shipment in the response carries the field, the whole response is the
        # order's shipments and we judge on all of them rather than guessing.
        matching = [
            shipment for shipment in shipments
            if normalize_id(shipment_order_number_of(shipment))
            == normalize_id(record.shipment_order_number)
        ]
        if not matching:
            if any(shipment_order_number_of(shipment) for shipment in shipments):
                return GateVerdict(
                    "fail",
                    f"getShipments for '{external_order_id}' returned {len(shipments)} "
                    f"shipment(s), none with shipmentOrderNumber "
                    f"{record.shipment_order_number}",
                    tenant_id, site_id,
                )
            matching = shipments

        items = [item for shipment in matching for item in shipment_items(shipment)]
        if not items:
            return GateVerdict(
                "fail",
                f"the shipment(s) for '{external_order_id}' carry no items to check the "
                "productId against",
                tenant_id, site_id, matching,
            )

        wanted = [item for item in items if item_matches_product(item, record.product_id)]
        if not wanted:
            return GateVerdict(
                "fail",
                f"productId '{record.product_id}' is not on shipment "
                f"{record.shipment_order_number}; it carries "
                + ", ".join(sorted({item_label(item) for item in items}))[:300],
                tenant_id, site_id, matching, len(items),
            )

        others = sorted({
            item_label(item) for item in items
            if not item_matches_product(item, record.product_id)
        })
        if others:
            return GateVerdict(
                "kibo_item",
                f"the shipment also carries {len(others)} other product(s) "
                f"({', '.join(others)[:200]}), so the warehouse was left alone and only "
                f"productId '{record.product_id}' was cancelled in Kibo",
                tenant_id, site_id, matching, len(items),
            )

        return GateVerdict(
            "warehouse",
            f"productId '{record.product_id}' is the only product on the shipment "
            f"({len(items)} item line(s))",
            tenant_id, site_id, matching, len(items),
        )


# ---------------------------------------------------------------------------
# BigQuery lookup — tlg-wlfs-prd
# ---------------------------------------------------------------------------

@dataclass
class OrderRow:
    shipment_order_number: str = ""
    logistic_order_number: str = ""
    location_name: str = ""
    brand: str = ""
    brand_code: str = ""


class OrderLookup:
    """shipmentOrderNumber -> the GX location rows of til.prd_orders."""

    def __init__(self) -> None:
        self.by_shipment: Dict[str, List[OrderRow]] = {}
        self.loaded = False

    def load(self, client: "bigquery.Client", shipment_order_numbers: List[str]) -> None:
        if not shipment_order_numbers:
            self.loaded = True
            return

        query = f"""
            SELECT
                o.shipmentOrderNumber AS shipmentOrderNumber,
                o.logisticOrderNumber AS logisticOrderNumber,
                o.locationName        AS locationName,
                o.brand               AS brand,
                bc.brandCode          AS brandCode
            FROM `{WLFS_PROJECT}.{WLFS_DATASET}.{ORDERS_TABLE}` AS o
            LEFT JOIN `{WLFS_PROJECT}.{WLFS_DATASET}.{BRAND_CONFIG_TABLE}` AS bc
                ON o.brand = bc.brand
            WHERE o.shipmentOrderNumber IN UNNEST(@numbers)
              AND o.locationName LIKE '%GX%'
        """
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ArrayQueryParameter("numbers", "STRING", shipment_order_numbers)
            ]
        )
        log(f"[BigQuery] Reading {ORDERS_TABLE} for {len(shipment_order_numbers)} "
            "shipmentOrderNumber(s) …")
        rows = client.query(query, job_config=job_config).result()

        count = 0
        for row in rows:
            son = clean(row.get("shipmentOrderNumber"))
            if not son:
                continue
            self.by_shipment.setdefault(normalize_id(son), []).append(
                OrderRow(
                    shipment_order_number=son,
                    logistic_order_number=clean(row.get("logisticOrderNumber")),
                    location_name=clean(row.get("locationName")),
                    brand=clean(row.get("brand")),
                    brand_code=clean(row.get("brandCode")),
                )
            )
            count += 1
        self.loaded = True
        log(f"[BigQuery] {count} GX row(s) for {len(self.by_shipment)} "
            "unique shipmentOrderNumber(s).")

    def for_shipment_order_number(self, value: str) -> List[OrderRow]:
        return list(self.by_shipment.get(normalize_id(value), []))


# ---------------------------------------------------------------------------
# GXO client
# ---------------------------------------------------------------------------

class LegacyTLSAdapter(HTTPAdapter):
    """
    The GXO endpoint negotiates legacy (unsafe) renegotiation, which OpenSSL 3
    refuses by default. OP_LEGACY_SERVER_CONNECT (0x4) allows it. The host also
    presents a certificate chain this runner cannot verify, so verification is
    off — the same compromise the original desktop script makes.
    """

    def init_poolmanager(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        context = create_urllib3_context()
        context.options |= 0x4
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        kwargs["ssl_context"] = context
        return super().init_poolmanager(*args, **kwargs)


class GxoClient:
    def __init__(self, url: str, api_key: str, timeout: float, max_retries: int,
                 update_reason: str) -> None:
        self.url = url
        self.api_key = api_key
        self.timeout = timeout
        self.max_retries = max_retries
        self.update_reason = update_reason
        self.session = requests.Session()
        self.session.mount("https://", LegacyTLSAdapter())
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    def build_payload(self, order: OrderRow, reason_message: str) -> Dict[str, Any]:
        return {
            "eventType": "CANCEL",
            "eventClass": "SHIPMENT",
            "eventId": str(uuid.uuid4()).upper(),
            "eventTimestamp": event_timestamp(),
            "data": {
                "shipmentOrderNumber": order.shipment_order_number,
                "logisticOrderNumber": order.logistic_order_number,
                # The warehouse calls this a locationCode; prd_orders calls it a
                # locationName. Same value, as in the original script.
                "locationCode": order.location_name,
                "brandCode": order.brand_code,
                "brand": order.brand,
                # Always empty: the warehouse reads an empty products array as
                # "the whole shipment", and that is the only thing it can act on.
                "products": [],
                "updateReason": self.update_reason,
                "reasonMessage": reason_message,
            },
        }

    def send(self, payload: Dict[str, Any]) -> Tuple[Optional[int], str, str]:
        """Return (status_code, error, response_excerpt)."""
        headers = {"Content-Type": "application/json", "api_key": self.api_key}
        last_error = ""
        for attempt in range(1, self.max_retries + 1):
            try:
                response = self.session.post(
                    self.url, headers=headers, json=payload,
                    timeout=self.timeout, verify=False,
                )
            except requests.RequestException as exc:
                last_error = str(exc)
                if attempt < self.max_retries:
                    time.sleep(retry_sleep_seconds(attempt))
                    continue
                return None, last_error, ""

            if response.status_code in RETRYABLE_STATUS_CODES and attempt < self.max_retries:
                time.sleep(retry_sleep_seconds(attempt, response))
                continue
            ok = 200 <= response.status_code < 300
            return (
                response.status_code,
                "" if ok else f"HTTP {response.status_code}",
                excerpt_of(response),
            )
        return None, last_error or "Unknown error", ""


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------

@dataclass
class Result:
    source_file: str = ""
    row_number: int = 0
    shipment_order_number: str = ""
    product_id: str = ""
    order_number: str = ""
    tenant_id: str = ""
    logistic_order_number: str = ""
    location_name: str = ""
    brand: str = ""
    brand_code: str = ""
    event_id: str = ""
    gate: str = ""
    action: str = ""
    oms_shipment_number: str = ""
    line_id: str = ""
    quantity: str = ""
    correlation_id: str = ""
    outcome: str = "failed"
    status_code: str = ""
    error: str = ""
    response_excerpt: str = ""

    @property
    def succeeded(self) -> bool:
        return self.outcome in SUCCESS_OUTCOMES

    @property
    def skipped(self) -> bool:
        return self.outcome in SKIP_OUTCOMES

    @staticmethod
    def of(record: InputRecord, **overrides: Any) -> "Result":
        result = Result(
            source_file=record.source_file,
            row_number=record.row_number,
            shipment_order_number=record.shipment_order_number,
            product_id=record.product_id,
            order_number=record.order_number,
            tenant_id=record.tenant_id,
        )
        for key, value in overrides.items():
            setattr(result, key, value)
        return result


@dataclass
class FileOutcome:
    path: Path
    read_error: str = ""
    results: List[Result] = field(default_factory=list)

    @property
    def has_failure(self) -> bool:
        return bool(self.read_error) or any(
            not (result.succeeded or result.skipped) for result in self.results
        )


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

class Processor:
    def __init__(self, lookup: OrderLookup, gate: Optional[ProductGate],
                 gxo: GxoClient, args: argparse.Namespace) -> None:
        self.lookup = lookup
        self.gate = gate
        self.gxo = gxo
        self.args = args

    # -- entry point --------------------------------------------------------

    def process_record(self, record: InputRecord) -> List[Result]:
        if not record.shipment_order_number:
            return [Result.of(record, outcome="failed",
                              error="the row has no shipmentOrderNumber")]

        gate_note = "no productId: the whole shipment is cancelled"
        if record.product_id:
            if self.gate is None:
                return [Result.of(record, outcome="failed",
                                  error="the row carries a productId but the Kibo check is "
                                        "disabled (--no-product-check)")]
            try:
                verdict = self.gate.check(record)
            except Exception as exc:  # noqa: BLE001  one bad record must not stop the run
                return [Result.of(record, outcome="failed",
                                  error=f"the product check raised {type(exc).__name__}: {exc}")]

            if verdict.decision == "fail":
                return [Result.of(record, outcome="failed", gate="failed",
                                  tenant_id=verdict.tenant_id, error=verdict.detail)]
            if verdict.decision == "kibo_item":
                # The warehouse hears nothing: the rest of the shipment ships.
                return self._cancel_item_in_kibo(record, verdict)
            gate_note = verdict.detail

        return self._cancel_at_warehouse(record, gate_note)

    # -- the warehouse path -------------------------------------------------

    def _cancel_at_warehouse(self, record: InputRecord, gate_note: str) -> List[Result]:
        rows = self.lookup.for_shipment_order_number(record.shipment_order_number)
        if not rows:
            return [Result.of(
                record, outcome="failed", action="cancel_at_warehouse",
                gate="whole shipment" if record.product_id else "",
                error=f"{record.shipment_order_number} has no GX location row in "
                      f"{WLFS_PROJECT}.{WLFS_DATASET}.{ORDERS_TABLE}",
            )]

        results: List[Result] = []
        for order in rows:
            result = Result.of(
                record,
                action="cancel_at_warehouse",
                gate="whole shipment" if record.product_id else "",
                logistic_order_number=order.logistic_order_number,
                location_name=order.location_name,
                brand=order.brand,
                brand_code=order.brand_code,
            )
            reason_message = clean(record.reason_message) or self.args.reason_message
            payload = self.gxo.build_payload(order, reason_message)
            result.event_id = payload["eventId"]

            if self.args.dry_run:
                result.outcome = "dry_run"
                result.error = f"{gate_note}; would POST {json.dumps(payload['data'])[:300]}"
                results.append(result)
                continue

            status_code, error, excerpt = self.gxo.send(payload)
            result.status_code = "" if status_code is None else str(status_code)
            result.response_excerpt = excerpt
            if error:
                result.outcome = "failed"
                result.error = error
            else:
                result.outcome = "success"
                result.error = gate_note if record.product_id else ""
            results.append(result)
            if self.args.sleep_between_requests > 0:
                time.sleep(self.args.sleep_between_requests)

        return results

    # -- the Kibo item path -------------------------------------------------

    def _cancel_item_in_kibo(self, record: InputRecord,
                             verdict: GateVerdict) -> List[Result]:
        """
        The shipment holds other products, so the warehouse is not told
        anything and only this product is cancelled in Kibo. One call per
        shipment that actually carries the product.
        """
        assert self.gate is not None
        carriers = [
            shipment for shipment in verdict.shipments
            if any(item_matches_product(item, record.product_id)
                   for item in shipment_items(shipment))
        ]
        results: List[Result] = []
        for shipment in carriers:
            results.append(self._cancel_item_on_shipment(record, verdict, shipment))
            if self.args.sleep_between_requests > 0:
                time.sleep(self.args.sleep_between_requests)
        if not results:  # the gate only returns kibo_item when something carries it
            results.append(Result.of(
                record, outcome="failed", action="cancel_item_in_kibo",
                gate="other products", tenant_id=verdict.tenant_id,
                error="no shipment in the getShipments response carries the product",
            ))
        return results

    def _cancel_item_on_shipment(self, record: InputRecord, verdict: GateVerdict,
                                 shipment: Dict[str, Any]) -> Result:
        assert self.gate is not None
        shipment_number = clean(shipment.get("shipmentNumber"))
        result = Result.of(
            record,
            action="cancel_item_in_kibo",
            gate="other products",
            tenant_id=verdict.tenant_id,
            oms_shipment_number=shipment_number,
            correlation_id=uuid.uuid4().hex,
        )
        if not shipment_number:
            result.outcome = "failed"
            result.error = "the shipment in the getShipments response has no shipmentNumber"
            return result

        status = shipment_status_of(shipment)
        if is_terminal_status(status):
            result.outcome = "already_terminal"
            result.error = f"the shipment is already {status}; nothing was cancelled"
            return result

        matches = [item for item in shipment_items(shipment)
                   if item_matches_product(item, record.product_id)]
        if not matches:
            canceled = shipment.get("canceledItems")
            already = [item for item in (canceled if isinstance(canceled, list) else [])
                       if isinstance(item, dict)
                       and item_matches_product(item, record.product_id)]
            result.outcome = "item_already_canceled" if already else "failed"
            result.error = (
                f"product {record.product_id} is already in canceledItems"
                if already else
                f"product {record.product_id} is not among the active items of "
                f"shipment {shipment_number}"
            )
            return result

        reason = {
            "reasonCode": clean(record.reason_code) or self.args.reason_code,
            "moreInfo": clean(record.reason_message) or self.args.reason_message,
        }
        wanted_quantity = int(record.quantity) if record.quantity.isdigit() else 0
        payload_items: List[Dict[str, Any]] = []
        line_ids: List[str] = []
        quantities: List[str] = []

        for item in matches:
            line_id = item.get("lineId")
            if line_id is None:
                result.outcome = "failed"
                result.error = (f"the matching item on shipment {shipment_number} "
                                "has no lineId")
                return result
            available = item.get("quantity")
            try:
                available_int = int(available)
            except (TypeError, ValueError):
                available_int = 0
            quantity = wanted_quantity if 0 < wanted_quantity <= available_int else available_int
            if quantity <= 0:
                result.outcome = "failed"
                result.error = (f"line {line_id} has no cancellable quantity "
                                f"(quantity={available})")
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
                f"{verdict.detail}; would cancel line(s) {result.line_id} "
                f"quantity {result.quantity} on Kibo shipment {shipment_number}, "
                "and send nothing to the warehouse"
            )
            return result

        ok, error, excerpt = self.gate.kibo.cancel_items(
            verdict.tenant_id, verdict.site_id, shipment_number,
            payload_items, self.args.send_notifications, result.correlation_id,
        )
        result.status_code = error.replace("HTTP ", "") if error.startswith("HTTP ") else ""
        result.response_excerpt = excerpt
        if ok:
            result.outcome = "item_canceled_in_kibo"
            result.error = verdict.detail
        else:
            result.outcome = "failed"
            result.error = f"{error} while cancelling the item in Kibo ({verdict.detail})"
        return result


# ---------------------------------------------------------------------------
# Logging and file moves
# ---------------------------------------------------------------------------

RESULT_FIELDS = [
    "source_file", "row_number", "shipment_order_number", "product_id", "order_number",
    "tenant_id", "action", "gate", "logistic_order_number", "location_name", "brand",
    "brand_code", "event_id", "oms_shipment_number", "line_id", "quantity",
    "outcome", "status_code", "error", "response_excerpt", "correlation_id",
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
    lines.append(f"Mode          : "
                 f"{'DRY RUN - nothing was sent to the warehouse' if args.dry_run else 'LIVE'}")
    lines.append(f"GXO endpoint  : {args.gxo_url}")
    lines.append(f"Product check : {'off' if args.no_product_check else 'on (Kibo getShipments)'}")
    lines.append(f"Files read    : {len(outcomes)}")
    sent = sum(1 for outcome in outcomes for r in outcome.results if r.outcome == "success")
    in_kibo = sum(1 for outcome in outcomes for r in outcome.results
                  if r.outcome == "item_canceled_in_kibo")
    lines.append(f"Records       : {succeeded + skipped + failed} "
                 f"({sent} sent to the warehouse, {in_kibo} cancelled in Kibo instead, "
                 f"{skipped} skipped, {failed} failed)")
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

        ok_rows = [r for r in outcome.results if r.outcome == "success"]
        kibo_rows = [r for r in outcome.results if r.outcome == "item_canceled_in_kibo"]
        skip_rows = [r for r in outcome.results if r.skipped]
        bad_rows = [r for r in outcome.results if not (r.succeeded or r.skipped)]

        def render(rows: List[Result], title: str) -> None:
            lines.append(f"  {title} ({len(rows)})")
            if not rows:
                lines.append("    -")
                return
            for row in rows:
                lines.append(
                    f"    row {row.row_number}: "
                    f"shipmentOrderNumber={row.shipment_order_number or '-'} "
                    f"productId={row.product_id or '-'}"
                )
                if row.action == "cancel_item_in_kibo":
                    lines.append(
                        f"      -> cancel_item_in_kibo shipment={row.oms_shipment_number or '-'} "
                        f"tenant={row.tenant_id or '-'} line={row.line_id or '-'} "
                        f"qty={row.quantity or '-'} outcome={row.outcome}"
                        + (f" HTTP {row.status_code}" if row.status_code else "")
                    )
                else:
                    lines.append(
                        f"      -> cancel_at_warehouse "
                        f"logisticOrderNumber={row.logistic_order_number or '-'} "
                        f"location={row.location_name or '-'} brand={row.brand or '-'}"
                        f"/{row.brand_code or '-'} outcome={row.outcome}"
                        + (f" HTTP {row.status_code}" if row.status_code else "")
                    )
                if row.error:
                    lines.append(f"         note: {row.error}")
                if row.response_excerpt and not row.succeeded:
                    lines.append(f"         response: {row.response_excerpt[:400]}")

        render(ok_rows, "SENT TO THE WAREHOUSE")
        lines.append("")
        render(kibo_rows, "CANCELLED IN KIBO INSTEAD (warehouse not told)")
        lines.append("")
        render(skip_rows, "SKIPPED (nothing was done)")
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
                row = {key: "" for key in RESULT_FIELDS}
                row.update({
                    "source_file": outcome.path.name,
                    "outcome": "failed",
                    "error": outcome.read_error,
                })
                writer.writerow(row)
                continue
            for result in outcome.results:
                data = asdict(result)
                writer.writerow({key: data.get(key, "") for key in RESULT_FIELDS})


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

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Send the warehouse cancellations in input/ to the GXO endpoint.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    # The Kibo worker owns input/, archive/, error/ and logs/ at the repository
    # root; this one lives under warehouse/ so the two never touch each other's
    # files, and both workflows can run at the same time.
    parser.add_argument("--input-dir", type=Path, default=Path("warehouse/input"))
    parser.add_argument("--archive-dir", type=Path, default=Path("warehouse/archive"))
    parser.add_argument("--error-dir", type=Path, default=Path("warehouse/error"))
    parser.add_argument("--log-dir", type=Path, default=Path("warehouse/logs"))
    parser.add_argument("--gxo-url", default=os.environ.get("GXO_URL") or DEFAULT_GXO_URL)
    parser.add_argument("--update-reason", default=DEFAULT_UPDATE_REASON)
    parser.add_argument("--reason-message", default=DEFAULT_REASON_MESSAGE,
                        help="data.reasonMessage on the warehouse event, and "
                             "canceledReason.moreInfo on a Kibo item cancellation.")
    parser.add_argument("--reason-code", default=DEFAULT_REASON_CODE,
                        help="canceledReason.reasonCode, used only when an item is "
                             "cancelled in Kibo.")
    parser.add_argument("--no-send-notifications", dest="send_notifications",
                        action="store_false",
                        help="Set sendNotifications to false on a Kibo item cancellation.")
    parser.set_defaults(send_notifications=True)
    parser.add_argument("--host", default=os.environ.get("KIBO_HOST") or DEFAULT_KIBO_HOST,
                        help="Kibo host suffix, used only by the product check.")
    parser.add_argument("--auth-url", default=DEFAULT_KIBO_AUTH_URL)
    parser.add_argument("--tenant-map", default="",
                        help='JSON object merged over the built-in brand map, '
                             'e.g. \'{"XY": "40001"}\'.')
    parser.add_argument("--default-tenant-id",
                        default=os.environ.get("KIBO_TENANT_ID") or DEFAULT_TENANT_ID)
    parser.add_argument("--default-site-id",
                        default=os.environ.get("KIBO_SITE_ID") or DEFAULT_SITE_ID)
    parser.add_argument("--no-product-check", action="store_true",
                        help="Do not read Kibo. Rows that carry a productId then fail rather "
                             "than cancelling a shipment that may hold other products.")
    parser.add_argument("--no-derive-external-order-id", dest="derive_external_order_id",
                        action="store_false",
                        help="Do not derive the externalOrderId from the shipmentOrderNumber "
                             "for the product check.")
    parser.set_defaults(derive_external_order_id=True)
    parser.add_argument("--dry-run", action="store_true",
                        help="Resolve and report, but send nothing to the warehouse.")
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
        log("DRY RUN: nothing will be sent to the warehouse.")

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
        lookup = OrderLookup()
        lookup.load(bigquery_client(), shipment_order_numbers)

        # 3. The Kibo client is only built when a row needs the product check.
        gate: Optional[ProductGate] = None
        if any(record.product_id for _, record in records) and not args.no_product_check:
            gate = ProductGate(
                kibo=KiboClient(
                    credentials=kibo_credentials(),
                    auth_url=args.auth_url,
                    host=args.host,
                    timeout=args.timeout,
                    max_retries=args.max_retries,
                ),
                tenant_map=load_tenant_map(args.tenant_map),
                default_tenant_id=args.default_tenant_id,
                default_site_id=args.default_site_id,
                derive_external=args.derive_external_order_id,
            )

        gxo = GxoClient(
            url=args.gxo_url,
            api_key=gxo_api_key(),
            timeout=args.timeout,
            max_retries=args.max_retries,
            update_reason=args.update_reason,
        )
        processor = Processor(lookup=lookup, gate=gate, gxo=gxo, args=args)

        for index, (outcome, record) in enumerate(records, start=1):
            log(f"[{index}/{len(records)}] {record.source_file} row {record.row_number}: "
                f"{record.label}")
            for result in processor.process_record(record):
                outcome.results.append(result)
                where = (
                    f"kibo shipment={result.oms_shipment_number or '-'} "
                    f"line={result.line_id or '-'}"
                    if result.action == "cancel_item_in_kibo" else
                    f"logisticOrderNumber={result.logistic_order_number or '-'} "
                    f"location={result.location_name or '-'}"
                )
                log(f"    {result.outcome.upper()} {where}"
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
    log(f"Summary: {succeeded} handled ({skipped} skipped, {failed} failed). "
        "See the log for the warehouse / Kibo split.")
    log(f"Log: {log_path}")
    log(f"Results CSV: {csv_path}")

    # A failed record is reported in the log and the file moves to error/, but the
    # job stays green so one bad record does not look like a broken workflow.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
