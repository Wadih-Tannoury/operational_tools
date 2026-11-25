#!/usr/bin/env python3
"""
KO monitoring — build the dashboard dataset.

A batch is a pair of files dropped in `ko_monitoring/input/` by the KO
monitoring console: the pending-monitoring CSV and the IMKO check workbook.
Both carry the same batch stamp, which is everything before the first `__` in
the file name, so a batch is one commit and one run:

    ko_monitoring/input/20261120-104501__Wadih__pending.csv
    ko_monitoring/input/20261120-104501__Wadih__check.xlsx

For each batch this worker:

 1. reads every row of the CSV (nothing is filtered out);
 2. reads the `imko` sheet of the workbook and keeps only the rows whose
    `ub_imko` is one of IMKO 001-026 or IMKO 100-199;
 3. joins the two on `VCVAIC` (workbook) = `Shipment` (CSV), collapsing the
    workbook's one-row-per-product down to one row per shipment, and fills the
    logistics order number in from OERODP where the CSV has none;
 4. calls the shipment-documents API once per shipment, exactly the way
    `Retreive_shipment_documents_and_tracking.py` does, and decides from the
    response whether the shipment's KO is fixed;
 5. reads `tlg-business-intelligence-prd.bi.wh_document_api_response` to find
    when the warehouse last tried to print the documents, and why it failed;
 6. writes one row per shipment to `ko_monitoring/output/`, and
 7. merges the same rows into
    `tlg-business-intelligence-prd.til.ko_operational_table`, which is what the
    dashboard reads: a shipment already there is updated rather than added
    again, and one that has dropped out of the uploads is marked
    `ko_resolved = TRUE` rather than deleted. See scripts/ko_operational_table.py.

ko_fix_date
-----------
Populated with the timestamp of the API call when the response says the
shipment can be closed and the warehouse has everything it needs to print:

    eligibleForPackageClosure is true, and
    the SHPLBL label is populated and in ZPL, and
    the shipping tracking number is populated, and
      either the RETLBL label is populated and in ZPL and the return tracking
      number is populated,
      or no RETLBL entry exists in the response at all.

A RETLBL entry that exists but carries no label data is the one case that is
deliberately NOT a fix: the return label was expected and is missing, so the
shipment stays KO and ko_fix_date is left empty.

last_tried_by_gxo_date / get_documents_error
--------------------------------------------
From `wh_document_api_response`, matching `shipment_order_number` and — when
the CSV supplies one — `logistics_order_number`. The newest `Succeeded` row
wins and the error is left empty. With no `Succeeded` row at all, the newest
row of any result code is used instead, and the error is its `error_message`,
falling back to its `result_code` when that is empty.

Environment
-----------
GET_DOCUMENTS_API_CREDENTIALS      {"clientId": "...", "clientSecret": "..."}
MODA_GET_DOCUMENTS_API_CREDENTIALS {"BEARER_TOKEN": "..."} — MODA's getlabel
BI_BIGQUERY_CREDS                  service-account JSON for the BI project
WLFS_BIGQUERY_CREDS                service-account JSON for the warehouse project
GET_DOCUMENTS_AUTH_URL             optional, overrides the token endpoint
GET_DOCUMENTS_URL                  optional, overrides the documents endpoint
MODA_GET_LABEL_URL                 optional, overrides the MODA endpoint
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import sys
import time
from base64 import b64decode
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    from openpyxl import load_workbook
except ImportError:  # pragma: no cover - reported at run time
    load_workbook = None  # type: ignore[assignment]

try:
    import ko_operational_table as table_module
except ImportError:  # pragma: no cover - the run still writes its CSV
    table_module = None  # type: ignore[assignment]

try:
    from google.cloud import bigquery
    from google.oauth2 import service_account
except ImportError:  # pragma: no cover - the run continues without BigQuery
    bigquery = None  # type: ignore[assignment]
    service_account = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

AUTH_URL = "https://integrations.thelevelgroup.com/logistics/v1/oauth2/token"
POST_URL = "https://integrations.thelevelgroup.com/logistics/v1/shipments/documents"
# MODA is a different warehouse behind a different endpoint: a bearer token
# rather than the OAuth2 dance, and one label per order.
MODA_URL = "https://integrations.thelevelgroup.com/warehouse/v1/moda/getlabel"

BQ_PROJECT = "tlg-business-intelligence-prd"
BQ_DATASET = "bi"
BQ_TABLE = "wh_document_api_response"
BQ_FQN = f"{BQ_PROJECT}.{BQ_DATASET}.{BQ_TABLE}"
BQ_SCOPES = ["https://www.googleapis.com/auth/bigquery"]

SHEET_NAME = "imko"

# The ub_imko codes this dashboard is about: 001-026 and 100-199. Written as
# numbers rather than as strings because the workbook spells them with a
# varying run of spaces ("IMKO  001", "IMKO   001"), which is normalised away
# before the comparison. Every other code — 027, 028, … — is dropped.
ALLOWED_IMKO = {f"IMKO {number:03d}" for number in list(range(1, 27)) + list(range(100, 200))}

# brand -> brandCode, as the warehouse numbers them. The brand is the first two
# characters of every shipment order number. Used only to build an OERODP for a
# shipment the workbook does not carry.
BRAND_CODES = {
    "DG": "15", "LN": "09", "MO": "41", "SC": "17", "CO": "19", "RC": "46",
    "WL": "12", "SW": "04", "CA": "05", "HB": "40", "BA": "07", "CV": "50",
    "OW": "51", "RL": "21", "VW": "23", "FA": "47", "DJ": "11", "AT": "24",
    "LA": "57", "CL": "30", "PL": "49", "HE": "33", "FE": "48", "CR": "31",
    "EF": "59", "FO": "34", "PJ": "35", "TM": "58", "FU": "37", "VE": "43",
    "ZH": "36", "WF": "56", "BO": "45", "MA": "52", "FL": "44", "PA": "53",
    "AL": "54", "BB": "42",
}

# The channel contributes its first letter: ecommerce -> E, Farfetch -> F,
# Miinto -> M. A channel listed here overrides that, and an empty string means
# no letter at all. GLAFAYETTE is here because the workbook spells that
# shipment's OERODP `11-DJ152574` rather than `11G-DJ152574` — checked against
# every shipment the two files share, which with this exception is 86 of 86.
CHANNEL_LETTERS = {
    "GLAFAYETTE": "",
}

# The workbook columns that are read. Everything else on the sheet is ignored.
XLSX_SHIPMENT_COLUMN = "VCVAIC"
XLSX_UB_COLUMN = "ub_imko"
XLSX_OERODP_COLUMN = "oerodp"

# How a CSV heading is recognised. Case, spaces, underscores, dots and hyphens
# are ignored, so "oms_shipment_number" and "oms_shipment_no" are the same heading.
CSV_ALIASES = {
    "shipment": "shipment",
    "shipmentordernumber": "shipment",
    "shipmentnumber": "shipment",
    "logisticorder": "logistic_order",
    "logisticordernumber": "logistic_order",
    "logisticsorder": "logistic_order",
    "logisticsordernumber": "logistic_order",
    "omsshipment": "oms_shipment",
    "omsshipmentno": "oms_shipment",
    "omsshipmentnumber": "oms_shipment",
    "omsshipment#": "oms_shipment",
    "orderdate": "order_date",
    "shipmentcreated": "shipment_created",
    "shipmentcreatedeffective": "shipment_created_effective",
    "action": "action",
    "activetask": "active_task",
    "externalorder": "external_order",
    "brand": "brand",
    "channel": "channel",
    "pendingdays": "pending_days",
    "locationname": "location_name",
    "whlastevent": "wh_last_event",
    "shiplabelerror": "ship_label_error",
    "returnlabelerror": "return_label_error",
    "shippingmethod": "shipping_method",
    "country": "country",
    # The detail export carries these, and the shorter export does not. They
    # are what makes the documents call match the shipment rather than a
    # one-parcel guess, and what says why the warehouse called it KO.
    "peso": "weight",
    "weight": "weight",
    "colli": "parcels",
    "parcels": "parcels",
    "parcelsnumber": "parcels",
    "gxokoreason": "gxo_ko_reason",
    "labelsucceeded": "label_succeeded",
    "trackingnumber": "csv_tracking_number",
    "returntracking": "csv_return_tracking",
    "carrier": "carrier",
    "status": "status",
    "wms": "wms",
}

# The dashboard dataset, in the order the dashboard shows it. The first block
# is what was asked for; the rest is the evidence behind ko_fix_date, kept so
# a row can be explained without re-running anything.
OUTPUT_COLUMNS = [
    "shipment_order_number",
    "oerodp",
    "ub_imko",
    "logistic_order_number",
    "oms_shipment_number",
    "order_submit_date",
    "shipment_creation_date",
    "action",
    "initial_fulfillment_status",
    "updated_fulfillment_status",
    "location_provider",
    "location_name",
    "ko_fixed",
    "ko_fix_date",
    "last_tried_by_gxo_date",
    "gxo_result_code",
    "tried_by_gxo_after_the_fix",
    "ko_reason",
    "ko_resolved",
    "ko_resolved_date",
    "source",
    "brand",
    "channel",
    "ko_source",
    "blocking_reason",
    "action_needed_from_gxo",
    "in_latest_gxo_input",
    "eligible_for_package_closure",
    "shipping_tracking_number",
    "return_tracking_number",
    "api_call_date",
    "batch",
]


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def stamp(moment: Optional[datetime] = None) -> str:
    return (moment or now_utc()).strftime("%Y%m%d-%H%M%S")


def iso(moment: Optional[datetime]) -> str:
    if moment is None:
        return ""
    if isinstance(moment, str):
        return moment
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalise_heading(value: Any) -> str:
    """
    A heading stripped to what identifies it.

    Case, spaces, underscores, hyphens, dots, '#', brackets and slashes all go,
    so '__DROP_EFFECTIVE__', 'shipment_created_effective' and
    'SHIPMENT CREATED EFFECTIVE' are one heading, and so are 'oms_shipment_number'
    and 'oms_shipment_number'.
    """
    return re.sub(r"[\s_\-.#()\[\]/]+", "", str(value or "").strip().lower())


def normalise_shipment(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "").strip()).upper()


def normalise_imko(value: Any) -> str:
    """'IMKO   001', 'imko 1', 'IMKO001' all become 'IMKO 001'."""
    text = re.sub(r"\s+", " ", str(value or "").strip().upper())
    match = re.match(r"^IMKO\s*0*(\d+)$", text)
    if match:
        return f"IMKO {int(match.group(1)):03d}"
    return text


def build_oerodp(shipment: Any, channel: Any, logistic_order: Any) -> str:
    """
    The OERODP for a shipment the workbook does not carry.

        brandCode + the channel's letter + '-' + the logistics order number

    `DJ-EUA00366315`, channel `ecommerce`, logistics order `DJ141651` gives
    `11E-DJ141651` — brand DJ is code 11, ecommerce contributes E, and the
    logistics order keeps its brand prefix here (unlike in the request body).

    Checked against every shipment the CSV and the workbook share: 86 of 86,
    with the one CHANNEL_LETTERS exception above. Returns nothing rather than a
    half-built code when the brand is unknown or the logistics order is
    missing, so a wrong OERODP never reaches the dashboard.
    """
    number = str(shipment or "").strip().upper()
    code = BRAND_CODES.get(number[:2], "")
    order = str(logistic_order or "").strip()
    if not code or not order:
        return ""
    name = str(channel or "").strip()
    letter = CHANNEL_LETTERS.get(name.upper(), name[:1].upper() if name else "")
    return f"{code}{letter}-{order}"


GXO = "GXO"
MODA = "MODA"
# Where a row came from. The first three are the upload sides; the fourth is a
# shipment the monitoring table reported as blocked on its own.
MONITORING_SOURCE = "MONITORING"


def location_provider_of(in_workbook: bool, csv_row: Dict[str, str]) -> str:
    """
    Whose warehouse the shipment is in, as far as the two files can say.

    The workbook is GXO's own check file, so a shipment that appears in it at
    all is GXO's whatever the CSV says. Failing that the CSV's WMS column
    answers it. When neither does — an empty WMS, which a fifth of the rows
    have — this returns nothing and the monitoring table is asked instead.
    """
    if in_workbook:
        return GXO
    wms = str(csv_row.get("wms", "") or "").strip().upper()
    if wms == GXO:
        return GXO
    if wms == MODA:
        return MODA
    return ""


def logistic_from_oerodp(value: Any) -> str:
    """
    The logistics order number that OERODP carries after its first hyphen.

    OERODP is a location code and the logistics order joined by one hyphen —
    '11E-DJ141651' is logistics order DJ141651, '51E-OW53695' is OW53695 — and
    that is the only place the number can be had for a shipment the workbook
    knows about and the CSV does not. Checked against the detail export on
    every shipment the two files share: 86 of 86 agreed.

    A shipment whose OERODP values derive to more than one logistics order
    returns nothing rather than a guess: the number is used to narrow a
    BigQuery match, and a wrong one costs the row its warehouse history.
    """
    derived = []
    for part in str(value or "").split(";"):
        piece = part.strip()
        if "-" in piece:
            tail = piece.split("-", 1)[1].strip()
            if tail and tail not in derived:
                derived.append(tail)
    return derived[0] if len(derived) == 1 else ""


def populated(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip() != ""
    if isinstance(value, (list, dict)):
        return len(value) > 0
    return True


def cell_text(value: Any) -> str:
    """A workbook or CSV cell as the dashboard should show it."""
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


class Log:
    """The run log: everything printed is also kept for the log file."""

    def __init__(self) -> None:
        self.lines: List[str] = []

    def __call__(self, message: str = "") -> None:
        print(message, flush=True)
        self.lines.append(message)

    def text(self) -> str:
        return "\n".join(self.lines) + "\n"


# ---------------------------------------------------------------------------
# ZPL
# ---------------------------------------------------------------------------

ZPL_MARKERS = ("^XA", "^XZ", "^FO", "^FD", "^GB", "~DG")

# What a label can be, and which of those the warehouse can actually print.
# ZPL goes to the label printer; PDF is printed as a page, which GXO does too.
# Anything else is a label nobody can put on a parcel.
# The only state in which asking the logistics endpoint for documents means
# anything: the shipment is waiting to be prepared. In any other state the
# documents either do not exist yet or are no longer the thing holding it up,
# so the call would cost a round trip to be told nothing. MODA's endpoint is
# not gated this way — it answers for a shipment whatever state Kibo has it in.
DOCUMENTS_STATUS = "prepare for shipment"


PRINTABLE = {
    "printable": ("ZPL", "PDF"),   # the default
    "zpl": ("ZPL",),               # the stricter reading
    "any": (),                     # format not checked at all
}


def declared_format(item: Dict[str, Any]) -> str:
    """Whatever the response itself says the label format is, if anything."""
    for key in ("labelFormat", "format", "documentFormat", "labelDataFormat",
                "printFormat", "fileFormat", "labelDataType", "contentType"):
        value = item.get(key)
        if populated(value):
            return str(value).strip().upper()
    return ""


def sniff_format(data: Any) -> str:
    """
    The format of some label data: 'ZPL', 'PDF', 'PNG', 'other' or ''.

    The field may hold the bytes raw or base64-encoded, so both are read. A ZPL
    stream opens with ^XA and is full of ^-prefixed commands, a PDF opens with
    %PDF, a PNG with its own signature — the three never collide.
    """
    if not populated(data):
        return ""
    text = data if isinstance(data, str) else str(data)
    candidates = [text]
    compact = re.sub(r"\s+", "", text)
    if len(compact) >= 8 and re.fullmatch(r"[A-Za-z0-9+/=]+", compact or "x"):
        try:
            decoded = b64decode(compact + "=" * (-len(compact) % 4), validate=False)
            candidates.append(decoded.decode("latin-1", errors="ignore"))
        except Exception:  # noqa: BLE001 - a field that will not decode is not base64
            pass
    for candidate in candidates:
        head = candidate.lstrip()[:4096]
        upper = head.upper()
        if upper.startswith("%PDF"):
            return "PDF"
        if head.startswith("\x89PNG"):
            return "PNG"
        if any(marker in upper for marker in ZPL_MARKERS):
            return "ZPL"
    return "other"


def label_format(item: Dict[str, Any]) -> str:
    """
    The format of one label document.

    The response's own field is believed first — it knows better than a sniff —
    and the data itself settles it when there is no such field.
    """
    stated = declared_format(item)
    for name in ("ZPL", "PDF", "PNG", "JPEG", "JPG", "EPL", "DPL"):
        if name in stated:
            return "ZPL" if name == "ZPL" else ("PDF" if name == "PDF" else name)
    return sniff_format(item.get("labelData"))


def printable(fmt: str, mode: str) -> bool:
    """Can the warehouse print a label in this format, under this mode?"""
    accepted = PRINTABLE.get(mode, PRINTABLE["printable"])
    if not accepted:               # mode 'any': a populated label is enough
        return bool(fmt)
    return fmt in accepted


# ---------------------------------------------------------------------------
# Reading the two input files
# ---------------------------------------------------------------------------

def read_csv(path: Path, log: Log) -> List[Dict[str, str]]:
    """Every row of the pending-monitoring CSV, headings mapped to keys."""
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
        log(f"  {path.name}: not UTF-8, read as Latin-1.")

    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel  # type: ignore[assignment]

    reader = csv.reader(io.StringIO(text), dialect)
    try:
        header = next(reader)
    except StopIteration:
        raise ValueError("the CSV has no header row")

    mapping: Dict[int, str] = {}
    for index, name in enumerate(header):
        key = CSV_ALIASES.get(normalise_heading(name))
        if key:
            mapping.setdefault(index, key)

    if "shipment" not in mapping.values():
        raise ValueError(
            "the CSV has no Shipment column. Found: " + ", ".join(str(h) for h in header)
        )

    rows: List[Dict[str, str]] = []
    for row in reader:
        if not row or all(not str(cell).strip() for cell in row):
            continue
        record: Dict[str, str] = {}
        for index, key in mapping.items():
            if index < len(row):
                record[key] = cell_text(row[index])
        if record.get("shipment"):
            rows.append(record)

    absent = [name for name in ("logistic_order", "oms_shipment", "shipment_created_effective")
              if name not in mapping.values()]
    if absent:
        log(f"  {path.name}: no column for {', '.join(absent)}; "
            "those stay empty on the dashboard.")
    log(f"  {path.name}: {len(rows)} row(s).")
    return rows


def read_workbook(path: Path, log: Log) -> List[Dict[str, str]]:
    """The imko sheet, filtered to the ub_imko codes this dashboard is about."""
    if load_workbook is None:
        raise ValueError("openpyxl is not installed; add it to requirements.txt")

    workbook = load_workbook(path, read_only=True, data_only=True)
    if SHEET_NAME not in workbook.sheetnames:
        raise ValueError(
            f"the workbook has no '{SHEET_NAME}' sheet. Found: "
            + ", ".join(workbook.sheetnames)
        )
    sheet = workbook[SHEET_NAME]
    stream = sheet.iter_rows(values_only=True)
    try:
        header = [cell_text(cell) for cell in next(stream)]
    except StopIteration:
        raise ValueError(f"the '{SHEET_NAME}' sheet is empty")

    wanted = {
        XLSX_SHIPMENT_COLUMN: "shipment",
        XLSX_UB_COLUMN: "ub_imko",
        XLSX_OERODP_COLUMN: "oerodp",
    }
    index: Dict[str, int] = {}
    for position, name in enumerate(header):
        for column, key in wanted.items():
            if normalise_heading(name) == normalise_heading(column):
                index.setdefault(key, position)

    missing = [column for column, key in wanted.items() if key not in index]
    if missing:
        raise ValueError(
            f"the '{SHEET_NAME}' sheet has no " + ", ".join(missing)
            + " column. Found: " + ", ".join(header)
        )

    rows: List[Dict[str, str]] = []
    total = 0
    for row in stream:
        if row is None or all(cell is None for cell in row):
            continue
        total += 1
        code = normalise_imko(row[index["ub_imko"]] if index["ub_imko"] < len(row) else "")
        if code not in ALLOWED_IMKO:
            continue
        shipment = cell_text(row[index["shipment"]] if index["shipment"] < len(row) else "")
        if not shipment:
            continue
        rows.append({
            "shipment": shipment,
            "ub_imko": code,
            "oerodp": cell_text(row[index["oerodp"]] if index["oerodp"] < len(row) else ""),
        })

    workbook.close()
    log(f"  {path.name}: {total} row(s) on '{SHEET_NAME}', {len(rows)} after the "
        f"ub_imko filter ({len(set(r['shipment'] for r in rows))} shipment(s)).")
    return rows


def collapse_workbook(rows: Sequence[Dict[str, str]]) -> Dict[str, Dict[str, str]]:
    """
    One row per shipment out of the workbook's one row per product.

    A shipment that carries several OERODP or ub_imko values keeps all of them,
    distinct and joined by '; ', rather than one being chosen silently.
    """
    collected: Dict[str, Dict[str, "OrderedDict[str, None]"]] = {}
    for row in rows:
        key = normalise_shipment(row["shipment"])
        entry = collected.setdefault(key, {
            "shipment": OrderedDict(), "oerodp": OrderedDict(), "ub_imko": OrderedDict(),
        })
        entry["shipment"].setdefault(row["shipment"].strip(), None)
        if row["oerodp"]:
            entry["oerodp"].setdefault(row["oerodp"], None)
        if row["ub_imko"]:
            entry["ub_imko"].setdefault(row["ub_imko"], None)
    return {
        key: {
            "shipment": next(iter(entry["shipment"]), ""),
            "oerodp": "; ".join(entry["oerodp"]),
            "ub_imko": "; ".join(entry["ub_imko"]),
        }
        for key, entry in collected.items()
    }


def collapse_csv(rows: Sequence[Dict[str, str]], log: Log) -> Dict[str, Dict[str, str]]:
    """One row per shipment out of the CSV. A repeated shipment keeps the first."""
    collected: "OrderedDict[str, Dict[str, str]]" = OrderedDict()
    duplicates = 0
    for row in rows:
        key = normalise_shipment(row.get("shipment"))
        if not key:
            continue
        if key in collected:
            duplicates += 1
            continue
        collected[key] = row
    if duplicates:
        log(f"  {duplicates} repeated shipment(s) in the CSV; the first row of each was kept.")
    return dict(collected)


# ---------------------------------------------------------------------------
# The documents API
# ---------------------------------------------------------------------------

def make_session(retries: int) -> requests.Session:
    session = requests.Session()
    policy = Retry(
        total=retries, backoff_factor=0.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=frozenset(["GET", "POST"]),
    )
    adapter = HTTPAdapter(max_retries=policy)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def api_credentials() -> Tuple[str, str]:
    """
    The client id and secret out of GET_DOCUMENTS_API_CREDENTIALS.

    The secret is the JSON the repository holds:
    {"clientId": "...", "clientSecret": "..."}. snake_case keys are accepted
    too, so a differently-spelled copy of the same secret still works.
    """
    raw = os.environ.get("GET_DOCUMENTS_API_CREDENTIALS", "").strip()
    if not raw:
        raise RuntimeError(
            "GET_DOCUMENTS_API_CREDENTIALS is not set. Add it as a repository "
            'secret: {"clientId": "...", "clientSecret": "..."}'
        )
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"GET_DOCUMENTS_API_CREDENTIALS is not valid JSON: {exc}")
    client_id = str(payload.get("clientId") or payload.get("client_id") or "").strip()
    secret = str(payload.get("clientSecret") or payload.get("client_secret") or "").strip()
    if not client_id or not secret:
        raise RuntimeError(
            "GET_DOCUMENTS_API_CREDENTIALS needs both clientId and clientSecret."
        )
    return client_id, secret


def moda_token() -> str:
    """
    The bearer token out of MODA_GET_DOCUMENTS_API_CREDENTIALS.

    The secret is the JSON the repository holds: {"BEARER_TOKEN": "..."}.
    Other spellings of the one key are accepted, so a differently-cased copy of
    the same secret still works. The MODA endpoint takes the token as it
    stands — there is no token call to make first.
    """
    raw = os.environ.get("MODA_GET_DOCUMENTS_API_CREDENTIALS", "").strip()
    if not raw:
        raise RuntimeError(
            "MODA_GET_DOCUMENTS_API_CREDENTIALS is not set. Add it as a "
            'repository secret: {"BEARER_TOKEN": "..."}'
        )
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"MODA_GET_DOCUMENTS_API_CREDENTIALS is not valid JSON: {exc}"
        )
    token = ""
    for key in ("BEARER_TOKEN", "bearer_token", "bearerToken", "token"):
        if str(payload.get(key) or "").strip():
            token = str(payload[key]).strip()
            break
    if not token:
        raise RuntimeError(
            "MODA_GET_DOCUMENTS_API_CREDENTIALS needs a BEARER_TOKEN."
        )
    # A token pasted with its scheme still works.
    return token[7:].strip() if token.lower().startswith("bearer ") else token


def get_access_token(session: requests.Session, auth_url: str) -> str:
    client_id, secret = api_credentials()
    response = session.post(
        auth_url,
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": secret,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=60,
    )
    response.raise_for_status()
    token = response.json().get("access_token")
    if not token:
        raise RuntimeError("the token endpoint returned no access_token")
    return token


def api_logistic_order(value: Any) -> str:
    """
    The logistics order number as the documents endpoint wants it: without its
    two-character brand prefix.

    `DJ141651` goes out as `141651`, `OW53695` as `53695` — which is the same
    number the detail export carries as `OMS shipment #`. The dashboard and the
    table keep the full value; only the request body is trimmed.
    """
    text = str(value or "").strip()
    return text[2:] if len(text) > 2 else text


def positive_number(value: Any, fallback: float) -> float:
    """A CSV cell as a number, falling back when it is empty, zero or not one."""
    text = str(value or "").strip().replace(",", ".")
    try:
        number = float(text)
    except ValueError:
        return fallback
    return number if number > 0 else fallback


def build_body(
    shipment: str, logistic_order: str,
    weight: Any = "", parcels: Any = "",
) -> Dict[str, Any]:
    """
    The body the endpoint documents, confirmed against Postman 2026-11-23.

    The order number goes out as `shipmentOrderNumber`. It was sent as
    `shipment_order_number` until 2026-11-23, and the endpoint answered HTTP
    200 with a full document envelope and eligibleForPackageClosure false
    every time — so the failure read as 351 shipments that were not ready
    rather than as a request the endpoint could not match. A 200 is not
    agreement; the field names are the agreement.

    brand is the two-letter prefix every shipment number opens with. weight and
    parcelsNumber come from the CSV's Peso and Colli when the export carries
    them and they are greater than zero, and fall back to the script's 1.0 and
    1 otherwise — a shipment the CSV records as weighing nothing is a gap in
    the export, not a weightless parcel, so the default is the safer body.

    carrierCode is left empty: this run reads what the warehouse would be
    given, it does not book anything, and the CSV's Carrier holds a name
    ("UPS") rather than the code the endpoint expects.

    logisticOrderNumber goes out without its two-character brand prefix —
    `DJ141651` as `141651` — which is what the endpoint expects. The full value
    stays on the dashboard and in the table.
    """
    number = str(shipment).strip()
    return {
        "shipmentOrderNumber": number,
        "brand": number[:2] if len(number) >= 2 else "",
        "weight": positive_number(weight, 1.0),
        "parcelsNumber": int(positive_number(parcels, 1)),
        "data": {
            "carrierCode": "",
            "logisticOrderNumber": api_logistic_order(logistic_order),
        },
    }


# The package-closure flag, in every spelling the API has been seen to use.
# It arrives camelCase — eligibleForPackageClosure — and the snake_case name
# is kept because that is what the column is called and what a hand-made
# payload tends to use. Everything hangs on this flag, so a spelling it does
# not recognise costs every shipment its fix.
ELIGIBLE_KEYS = (
    "eligibleForPackageClosure",
    "eligible_for_package_closure",
    "EligibleForPackageClosure",
    "eligibleforpackageclosure",
)


def body_variants(shipment: str, logistic_order: str,
                  weight: Any = "", parcels: Any = "") -> List[Tuple[str, Dict[str, Any]]]:
    """
    The body this code sends, and the near-misses it could be instead.

    Only for --probe-variants. A shipment can come back eligible in Postman
    and not eligible here, and when it does the difference is in the request,
    not the reading — so rather than renaming a field on a hunch and waiting a
    day to see, this sends the candidates one after another against the same
    shipment and prints what each one gets back.

    The candidates are the two differences between our body and the response a
    successful call echoes: the case of the order-number key, and whether the
    logistics order number is sent at all.
    """
    number = str(shipment).strip()
    brand = number[:2] if len(number) >= 2 else ""
    weight_value = positive_number(weight, 1.0)
    parcels_value = int(positive_number(parcels, 1))
    stripped = api_logistic_order(logistic_order)
    full = str(logistic_order or "").strip()

    def body(key: str, logistic: Optional[str]) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            key: number,
            "brand": brand,
            "weight": weight_value,
            "parcelsNumber": parcels_value,
        }
        data: Dict[str, Any] = {"carrierCode": ""}
        if logistic is not None:
            data["logisticOrderNumber"] = logistic
        payload["data"] = data
        return payload

    variants = [
        ("as sent today", body("shipmentOrderNumber", stripped)),
        ("snake_case key, as sent before 2026-11-23",
         body("shipment_order_number", stripped)),
        ("no logisticOrderNumber", body("shipmentOrderNumber", None)),
        ("snake_case key, no logisticOrderNumber",
         body("shipment_order_number", None)),
        ("empty logisticOrderNumber", body("shipmentOrderNumber", "")),
    ]
    if full and full != stripped:
        variants.append(("logisticOrderNumber with its brand prefix",
                         body("shipmentOrderNumber", full)))
    return variants


def read_eligible(payload: Dict[str, Any]) -> Optional[bool]:
    """
    The package-closure flag, whatever it is called and however it is typed.

    True, False, or None when the response does not carry it at all — and
    None is not False: it means the API said nothing, which judge() treats as
    not eligible but ko_status reports differently.
    """
    value = None
    for key in ELIGIBLE_KEYS:
        if key in payload:
            value = payload[key]
            break
    else:
        # Some responses nest it under data; cheap to look, cheap to be wrong.
        nested = payload.get("data")
        if isinstance(nested, dict):
            for key in ELIGIBLE_KEYS:
                if key in nested:
                    value = nested[key]
                    break
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    # "true" / "True" / 1 all mean the same thing here.
    text = str(value).strip().lower()
    if text in ("true", "1", "yes"):
        return True
    if text in ("false", "0", "no"):
        return False
    return None


def read_response(payload: Dict[str, Any], zpl_mode: str) -> Dict[str, Any]:
    """Everything ko_fix_date is decided on, read out of one API response."""
    labels = payload.get("labelDocuments") or []
    documents = payload.get("invoiceDocuments") or []

    result = {
        "eligible": read_eligible(payload),
        "shplbl_present": False, "shplbl_populated": False,
        "shplbl_format": "", "shplbl_printable": False,
        "retlbl_present": False, "retlbl_populated": False,
        "retlbl_format": "", "retlbl_printable": False,
        "shipping_tracking": "", "return_tracking": "",
        "inv": False, "rpi": False,
    }

    for item in labels:
        if not isinstance(item, dict):
            continue
        label_type = str(item.get("labelType") or "").strip().upper()
        if label_type == "SHPLBL":
            result["shplbl_present"] = True
            if populated(item.get("labelData")):
                result["shplbl_populated"] = True
                fmt = label_format(item)
                result["shplbl_format"] = result["shplbl_format"] or fmt
                result["shplbl_printable"] = (
                    result["shplbl_printable"] or printable(fmt, zpl_mode)
                )
            if not result["shipping_tracking"] and populated(item.get("trackingNumber")):
                result["shipping_tracking"] = str(item.get("trackingNumber")).strip()
        elif label_type == "RETLBL":
            # Presence is what the third rule turns on: a RETLBL entry that
            # exists but carries no data means the return label was expected
            # and is missing, which is not a fix.
            result["retlbl_present"] = True
            if populated(item.get("labelData")):
                result["retlbl_populated"] = True
                fmt = label_format(item)
                result["retlbl_format"] = result["retlbl_format"] or fmt
                result["retlbl_printable"] = (
                    result["retlbl_printable"] or printable(fmt, zpl_mode)
                )
            if not result["return_tracking"] and populated(item.get("trackingNumber")):
                result["return_tracking"] = str(item.get("trackingNumber")).strip()

    for item in documents:
        if not isinstance(item, dict):
            continue
        doc_type = str(item.get("docType") or "").strip().upper()
        if doc_type == "INV":
            result["inv"] = result["inv"] or populated(item.get("link"))
        elif doc_type == "RPI":
            result["rpi"] = result["rpi"] or populated(item.get("link"))

    return result


def judge(flags: Dict[str, Any]) -> Tuple[bool, str]:
    """
    (fixed, why). The three rules, in the order they were given.

    Everything hangs on eligibleForPackageClosure, a shipping label that is
    there and is ZPL, and a shipping tracking number. What the return label
    does then decides between the second and the third rule.
    """
    if flags["eligible"] is None:
        return False, "the response carries no package-closure flag"
    if flags["eligible"] is not True:
        return False, "not eligible for package closure"
    if not flags["shplbl_populated"]:
        return False, "no shipping label"
    if not flags["shplbl_printable"]:
        return False, (
            "the shipping label is "
            + (f"{flags['shplbl_format']}, which the warehouse cannot print"
               if flags["shplbl_format"] else "in an unreadable format")
        )
    if not flags["shipping_tracking"]:
        return False, "no shipping tracking number"

    if flags["retlbl_populated"]:
        if not flags["retlbl_printable"]:
            return False, (
                "the return label is "
                + (f"{flags['retlbl_format']}, which the warehouse cannot print"
                   if flags["retlbl_format"] else "in an unreadable format")
            )
        if not flags["return_tracking"]:
            return False, "no return tracking number"
        return True, (
            f"fixed: shipping label {flags['shplbl_format']}, return label "
            f"{flags['retlbl_format']}, both tracking numbers"
        )

    if flags["retlbl_present"]:
        return False, "a RETLBL entry exists but carries no label"
    return True, (
        f"fixed: shipping label {flags['shplbl_format']}, no return label expected"
    )


# MODA's response has not been pinned down to one shape, so the reader below
# looks for a label anywhere in the body rather than at one fixed path. These
# are the key names worth looking at; anything else is passed over.
MODA_LABEL_KEYS = (
    "labeldata", "label", "labels", "labelcontent", "zpl", "zpldata", "content",
    "base64", "base64content", "document", "documentdata", "file", "filecontent",
    "pdf", "data", "body", "printdata",
)
MODA_TRACKING_KEYS = (
    "trackingnumber", "tracking", "trackingcode", "trackingid", "parcelnumber",
    "awb", "awbnumber",
)
MODA_ERROR_KEYS = ("error", "errormessage", "message", "detail", "details",
                   "description", "reason")
# A label is a long string. A short one under a label-ish key is a file name, a
# format word or an id, none of which is something the warehouse can print.
MODA_MIN_LABEL_CHARS = 64


def _walk(node: Any, depth: int = 0):
    """Every (key, value) pair in a JSON body, however deeply nested."""
    if depth > 8:
        return
    if isinstance(node, dict):
        for key, value in node.items():
            yield str(key).strip().lower(), value
            yield from _walk(value, depth + 1)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item, depth + 1)


def read_moda_response(payload: Dict[str, Any], zpl_mode: str) -> Dict[str, Any]:
    """
    Everything ko_fix_date is decided on, read out of one MODA getlabel body.

    MODA returns one label for the order, not the SHPLBL/RETLBL pair the
    logistics endpoint returns, so the label it does return is recorded in the
    shipping label's columns and the return ones stay empty. The body is
    searched rather than indexed: a label is a long string under a label-ish
    key, a tracking number a short one under a tracking-ish key.
    """
    result = {
        "eligible": None,
        "shplbl_present": False, "shplbl_populated": False,
        "shplbl_format": "", "shplbl_printable": False,
        "retlbl_present": False, "retlbl_populated": False,
        "retlbl_format": "", "retlbl_printable": False,
        "shipping_tracking": "", "return_tracking": "",
        "inv": False, "rpi": False,
        "stated_format": "", "error": "",
    }
    if not isinstance(payload, dict):
        return result

    for key, value in _walk(payload):
        if isinstance(value, (dict, list)):
            continue
        text = str(value or "").strip()
        if not text:
            continue
        if key in MODA_TRACKING_KEYS and not result["shipping_tracking"]:
            result["shipping_tracking"] = text[:120]
        elif key in ("labelformat", "format", "documentformat", "contenttype"):
            result["stated_format"] = result["stated_format"] or text.upper()
        elif key in MODA_ERROR_KEYS and not result["error"]:
            result["error"] = text[:400]
        elif key in MODA_LABEL_KEYS and len(re.sub(r"\s+", "", text)) >= MODA_MIN_LABEL_CHARS:
            result["shplbl_present"] = True
            if not result["shplbl_populated"]:
                result["shplbl_populated"] = True
                fmt = sniff_format(text)
                stated = result["stated_format"]
                for name in ("ZPL", "PDF", "PNG"):
                    if name in stated:
                        fmt = name
                        break
                result["shplbl_format"] = fmt
                result["shplbl_printable"] = printable(fmt, zpl_mode)
    return result


def judge_moda(flags: Dict[str, Any]) -> Tuple[bool, str]:
    """
    (fixed, why) for a MODA shipment.

    There is no package-closure flag and no return label to weigh here: the KO
    is fixed when the warehouse is handed a label it can print.
    """
    if not flags["shplbl_populated"]:
        return False, ("no label returned"
                       + (f": {flags['error']}" if flags["error"] else ""))
    if not flags["shplbl_printable"]:
        return False, (
            "the label is "
            + (f"{flags['shplbl_format']}, which the warehouse cannot print"
               if flags["shplbl_format"] else "in an unreadable format")
        )
    return True, f"fixed: label {flags['shplbl_format']}"


def call_moda_label(
    session: requests.Session, token: str, url: str, shipment: str, zpl_mode: str,
) -> Dict[str, Any]:
    """
    One MODA shipment, one call. Nothing here raises: a failure is a row too.

    The body is the shipment number and a parcel of one — weight and
    packagesNumber are always 1 for this check, which asks whether a label can
    be produced at all, not what the real parcel weighs.
    """
    called_at = now_utc()
    outcome: Dict[str, Any] = {
        "called_at": called_at, "http_status": "", "message": "", "create_error": "",
        "flags": read_moda_response({}, zpl_mode), "fixed": False, "why": "",
    }
    try:
        response = session.post(
            url,
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json"},
            json={"orderNumber": str(shipment).strip(), "weight": 1,
                  "packagesNumber": 1},
            timeout=120,
        )
    except requests.RequestException as exc:
        outcome["why"] = f"the call failed: {exc}"
        outcome["message"] = str(exc)
        return outcome

    outcome["http_status"] = response.status_code
    payload: Optional[Dict[str, Any]] = None
    try:
        parsed = response.json()
        payload = parsed if isinstance(parsed, dict) else None
    except ValueError:
        outcome["message"] = (response.text or response.reason or "")[:400]

    if payload is None:
        if not outcome["message"]:
            outcome["message"] = response.reason or ""
        outcome["why"] = f"HTTP {response.status_code}: no JSON body"
        return outcome

    flags = read_moda_response(payload, zpl_mode)
    fixed, why = judge_moda(flags)
    outcome["flags"] = flags
    outcome["fixed"] = fixed
    outcome["why"] = why
    outcome["message"] = (flags["error"] or response.reason or "")[:400]
    outcome["create_error"] = flags["error"] if not fixed else ""
    if not (200 <= response.status_code < 300) and not fixed:
        outcome["why"] = f"HTTP {response.status_code}: {outcome['why'] or outcome['message']}"
    return outcome


def call_documents_api(
    session: requests.Session, token: str, url: str, shipment: str,
    logistic_order: str, zpl_mode: str, weight: Any = "", parcels: Any = "",
) -> Dict[str, Any]:
    """One shipment, one call. Nothing here raises: a failure is a row too."""
    called_at = now_utc()
    outcome: Dict[str, Any] = {
        "called_at": called_at, "http_status": "", "message": "", "create_error": "",
        "flags": read_response({}, zpl_mode), "fixed": False, "why": "",
    }
    body = build_body(shipment, logistic_order, weight, parcels)
    outcome["request"] = body
    try:
        response = session.post(
            url,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=body,
            timeout=120,
        )
    except requests.RequestException as exc:
        outcome["why"] = f"the call failed: {exc}"
        outcome["message"] = str(exc)
        return outcome

    outcome["http_status"] = response.status_code
    payload: Optional[Dict[str, Any]] = None
    try:
        parsed = response.json()
        payload = parsed if isinstance(parsed, dict) else None
    except ValueError:
        outcome["message"] = (response.text or response.reason or "")[:400]

    if payload is None:
        if not outcome["message"]:
            outcome["message"] = response.reason or ""
        outcome["why"] = f"HTTP {response.status_code}: no JSON body"
        return outcome

    outcome["message"] = str(payload.get("message", "") or response.reason or "")[:400]

    data = payload.get("data")
    if isinstance(data, list) and data and isinstance(data[0], dict):
        first = data[0]
        shipment_block = first.get("shipment") if isinstance(first.get("shipment"), dict) else {}
        outcome["create_error"] = str(
            first.get("createError") or (shipment_block or {}).get("errorDetails") or ""
        )[:400]

    outcome["response_keys"] = sorted(payload.keys())
    flags = read_response(payload, zpl_mode)
    fixed, why = judge(flags)
    outcome["flags"] = flags
    outcome["fixed"] = fixed
    outcome["why"] = why
    if not (200 <= response.status_code < 300) and not fixed:
        outcome["why"] = f"HTTP {response.status_code}: {outcome['why'] or outcome['message']}"
    return outcome


# ---------------------------------------------------------------------------
# BigQuery: what the warehouse last tried
# ---------------------------------------------------------------------------

def bigquery_client(log: Log) -> Optional[Any]:
    if bigquery is None or service_account is None:
        log("  The BigQuery libraries are not installed; the warehouse lookup and the "
            "KO operational table are both skipped.")
        return None
    raw = os.environ.get("BI_BIGQUERY_CREDS", "").strip()
    if not raw:
        log("  BI_BIGQUERY_CREDS is not set; the warehouse lookup and the KO "
            "operational table are both skipped.")
        return None
    try:
        info = json.loads(raw)
        credentials = service_account.Credentials.from_service_account_info(info, scopes=BQ_SCOPES)
        return bigquery.Client(project=BQ_PROJECT, credentials=credentials)
    except Exception as exc:  # noqa: BLE001 - a run without the lookup is still a run
        log(f"  Could not sign in to BigQuery: {exc}")
        return None


def wlfs_bigquery_client(log: Log) -> Optional[Any]:
    """
    The warehouse's own project, for monitoring.gxo_ko and prd_shipmentStatus.

    A separate service account from the BI one, under WLFS_BIGQUERY_CREDS —
    the same secret the cancellation workers already use.
    """
    if bigquery is None or service_account is None:
        return None
    raw = os.environ.get("WLFS_BIGQUERY_CREDS", "").strip()
    if not raw:
        log("  WLFS_BIGQUERY_CREDS is not set; gxo_ko and the SHIPPED check are skipped.")
        return None
    try:
        info = json.loads(raw)
        credentials = service_account.Credentials.from_service_account_info(
            info, scopes=BQ_SCOPES)
        return bigquery.Client(
            project=str(info.get("project_id") or table_module.WLFS_PROJECT),
            credentials=credentials)
    except Exception as exc:  # noqa: BLE001
        log(f"  Could not sign in to the warehouse project: {exc}")
        return None


def fetch_gxo_attempts(
    client: Any, keys: Sequence[Tuple[str, str]], lookback_days: int, log: Log,
) -> Dict[str, Dict[str, Any]]:
    """
    For each shipment, the newest Succeeded attempt and the newest attempt of
    any result code, out of `wh_document_api_response`.

    The logistics order number narrows the match only for the shipments whose
    CSV row carries one; for the rest the shipment number alone is the key, so
    a missing column costs coverage of the join and never a row.
    """
    if not keys:
        return {}

    numbers = sorted({shipment for shipment, _ in keys})
    pairs = sorted({(shipment, logistic) for shipment, logistic in keys if logistic})

    where = ["UPPER(TRIM(CAST(shipment_order_number AS STRING))) IN UNNEST(@numbers)"]
    parameters: List[Any] = [
        bigquery.ArrayQueryParameter("numbers", "STRING", numbers),
    ]
    if lookback_days > 0:
        where.append("created_on >= @since")
        parameters.append(bigquery.ScalarQueryParameter(
            "since", "TIMESTAMP", now_utc() - timedelta(days=lookback_days)
        ))

    sql = f"""
        SELECT
          UPPER(TRIM(CAST(shipment_order_number AS STRING)))            AS shipment,
          UPPER(TRIM(CAST(COALESCE(logistics_order_number, '') AS STRING))) AS logistic,
          created_on,
          CAST(result_code   AS STRING) AS result_code,
          CAST(error_message AS STRING) AS error_message
        FROM `{BQ_PROJECT}.{BQ_DATASET}.{BQ_TABLE}`
        WHERE {' AND '.join(where)}
        ORDER BY created_on DESC
    """

    try:
        job = client.query(sql, job_config=bigquery.QueryJobConfig(query_parameters=parameters))
        rows = list(job.result())
    except Exception as exc:  # noqa: BLE001
        log(f"  The {BQ_FQN} lookup failed: {exc}")
        return {}

    wanted_logistic = {shipment: logistic for shipment, logistic in pairs}
    found: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        shipment = str(row["shipment"] or "")
        expected = wanted_logistic.get(shipment, "")
        if expected and str(row["logistic"] or "") != expected:
            continue
        entry = found.setdefault(shipment, {"succeeded": None, "any": None})
        if entry["any"] is None:
            entry["any"] = row
        if entry["succeeded"] is None and str(row["result_code"] or "").strip().upper() == "SUCCEEDED":
            entry["succeeded"] = row

    log(f"  {BQ_FQN}: {len(rows)} row(s) read, {len(found)} shipment(s) matched.")
    return found


def gxo_columns(entry: Optional[Dict[str, Any]]) -> Dict[str, str]:
    """
    last_tried_by_gxo_date, get_documents_error, the result code behind them,
    and the logistics order number that attempt carried.

    That last one is why this returns more than it was asked for: the
    pending-monitoring export has no Logistic order column, so the only place
    the number can be had is the warehouse's own attempt row. A CSV that does
    carry the column keeps its own value; this only fills a gap.
    """
    empty = {"last_tried": "", "error": "", "result_code": "", "logistic": ""}
    if not entry:
        return empty
    succeeded = entry.get("succeeded")
    if succeeded is not None:
        return {
            "last_tried": iso(succeeded["created_on"]),
            "error": "",
            "result_code": str(succeeded["result_code"] or ""),
            "logistic": str(succeeded["logistic"] or ""),
        }
    latest = entry.get("any")
    if latest is None:
        return empty
    message = str(latest["error_message"] or "").strip()
    code = str(latest["result_code"] or "").strip()
    return {
        "last_tried": iso(latest["created_on"]),
        "error": message or code,
        "result_code": code,
        "logistic": str(latest["logistic"] or ""),
    }


# ---------------------------------------------------------------------------
# Batches
# ---------------------------------------------------------------------------

def batch_key(path: Path) -> str:
    """The stamp the console puts in front of both files of a batch."""
    return path.name.split("__", 1)[0]


def find_batches(input_dir: Path) -> Tuple[List[Tuple[str, Optional[Path], Optional[Path]]],
                                           List[Tuple[str, List[Path]]]]:
    """
    (batches to process, batches that make no sense).

    A batch is a CSV, a workbook, or one of each. Either file on its own is a
    whole batch: the CSV is TLG's pending-monitoring list and the workbook is
    GXO's check file, and each says something the other does not, so there is
    no reason to make one wait for the other. A run with only the workbook
    adds and re-checks GXO's shipments; a run with only the CSV does the same
    for TLG's, and archives no GXO shipment for absence, because a workbook
    that was never uploaded is not evidence that a shipment has gone.

    A batch is only incomplete now if it has two of the same kind — two CSVs
    or two workbooks under one key — which means the upload went wrong rather
    than that something is missing.
    """
    grouped: "OrderedDict[str, List[Path]]" = OrderedDict()
    for path in sorted(input_dir.glob("*")):
        if path.is_file() and not path.name.startswith("."):
            grouped.setdefault(batch_key(path), []).append(path)

    complete: List[Tuple[str, Optional[Path], Optional[Path]]] = []
    incomplete: List[Tuple[str, List[Path]]] = []
    for key, files in grouped.items():
        sheets = [p for p in files if p.suffix.lower() in (".xlsx", ".xlsm")]
        tables = [p for p in files if p.suffix.lower() in (".csv", ".txt")]
        if len(sheets) <= 1 and len(tables) <= 1 and (sheets or tables):
            complete.append((key, tables[0] if tables else None,
                             sheets[0] if sheets else None))
        else:
            incomplete.append((key, files))
    return complete, incomplete


# ---------------------------------------------------------------------------
# One batch
# ---------------------------------------------------------------------------

def build_rows(
    csv_rows: Dict[str, Dict[str, str]],
    xlsx_rows: Dict[str, Dict[str, str]],
    batch: str,
    join: str = "outer",
    gxo_ko: Optional[Dict[str, str]] = None,
) -> List[Dict[str, str]]:
    """
    One row per shipment. `source` says which side a row came from, so a
    shipment only one of the two files knows about is visible rather than
    quietly dropped.

    join 'outer' every CSV shipment, plus the ones the filtered workbook adds
    join 'csv'   every CSV shipment and nothing else
    join 'inner' only the shipments both files carry
    """
    # The GXO side is the workbook plus the warehouse's own KO list: a
    # shipment on either is GXO's, and gxo_ko regularly carries ones the
    # workbook does not.
    gxo_ko = gxo_ko or {}
    gxo_side = dict(xlsx_rows)
    for key, logistic in gxo_ko.items():
        if key not in gxo_side:
            gxo_side[key] = {"shipment": key, "oerodp": "", "ub_imko": "",
                             "logistic": logistic}

    if join == "csv":
        order = list(csv_rows.keys())
    elif join == "inner":
        order = [key for key in csv_rows if key in gxo_side]
    else:
        order = list(csv_rows.keys()) + [key for key in gxo_side if key not in csv_rows]
    rows: List[Dict[str, str]] = []
    for key in order:
        csv_row = csv_rows.get(key, {})
        xlsx_row = gxo_side.get(key, {})
        number = csv_row.get("shipment") or xlsx_row.get("shipment") or key
        # TLG for the pending-monitoring CSV, GXO for the warehouse's own
        # workbook — the systems the two files come from, rather than their
        # file formats.
        if csv_row and xlsx_row:
            source = "TLG + GXO"
        elif csv_row:
            source = "TLG only"
        else:
            source = "GXO only"
        rows.append({
            "shipment_order_number": number,
            # The workbook's value where there is one; otherwise built from the
            # brand code, the channel and the logistics order number.
            "oerodp": xlsx_row.get("oerodp", "") or build_oerodp(
                number, csv_row.get("channel", ""), csv_row.get("logistic_order", "")
            ),
            "ub_imko": xlsx_row.get("ub_imko", ""),
            # The CSV first; failing that, what OERODP carries. An excel-only
            # shipment has no CSV row at all, so this is the only source it has.
            "logistic_order_number": (
                csv_row.get("logistic_order", "")
                or logistic_from_oerodp(xlsx_row.get("oerodp", ""))
                or xlsx_row.get("logistic", "")
            ),
            "oms_shipment_number": csv_row.get("oms_shipment", ""),
            "order_submit_date": csv_row.get("order_date", ""),
            "shipment_creation_date": csv_row.get("shipment_created", ""),
            "action": csv_row.get("action", ""),
            # The CSV's active task is what the shipment was doing when it
            # was listed, which is the first-seen status. The live one is
            # Kibo's, and arrives in refresh_fulfillment.
            "initial_fulfillment_status": csv_row.get("active_task", ""),
            "updated_fulfillment_status": "",
            # GXO when the workbook knows the shipment at all, or when the CSV
            # says so; MODA when the CSV says that. Anything else is left for
            # the monitoring table to answer.
            "location_provider": location_provider_of(bool(xlsx_row), csv_row),
            "location_name": csv_row.get("location_name", ""),
            "ko_fixed": "no",
            "ko_fix_date": "",
            "last_tried_by_gxo_date": "",
            "gxo_result_code": "",
            "tried_by_gxo_after_the_fix": "no",
            "ko_reason": "",
            "ko_resolved": "",
            "ko_resolved_date": "",
            "source": source,
            "brand": csv_row.get("brand", "") or number[:2],
            "channel": csv_row.get("channel", ""),
            # Set by a pass of its own after the write, over the whole table.
            "in_latest_gxo_input": "",
            "eligible_for_package_closure": "",
            "shipping_tracking_number": "",
            "return_tracking_number": "",
            "api_call_date": "",
            "batch": batch,
            # Used while the run works, not written to the dataset.
            "_ko_status": "",
            "_weight": csv_row.get("weight", ""),
            "_parcels": csv_row.get("parcels", ""),
        })
    return rows


def yes_no(value: Any) -> str:
    if value is None or value == "":
        return ""
    return "yes" if value else "no"


def skip_already_fixed(rows: List[Dict[str, str]], args: argparse.Namespace,
                       log: Log, client: Any) -> Dict[str, Dict[str, str]]:
    """
    Carry the stored evidence onto the shipments the table already calls fixed.

    A shipment the table records as fixed is not asked again: the KO is fixed,
    and the documents call would only cost a request and risk overwriting the
    evidence of how it was fixed.
    """
    already: Dict[str, Dict[str, str]] = {}
    if client is not None:
        already = table_module.fetch_fixed(
            client, [normalise_shipment(row["shipment_order_number"]) for row in rows], log,
        )
        for row in rows:
            stored = already.get(normalise_shipment(row["shipment_order_number"]))
            if not stored:
                continue
            for column, value in stored.items():
                if value not in (None, "") and column in row:
                    row[column] = value
            row["ko_fixed"] = "yes"
        if already:
            log(f"  {len(already)} shipment(s) already fixed; they are not asked again.")
    return already


# ---------------------------------------------------------------------------
# Kibo: what a GXO shipment is actually waiting on
# ---------------------------------------------------------------------------
# A shipment the table has no fulfilment status for — or one in a status other
# than "Prepare for Shipment" — tells us nothing about whether its documents
# are worth asking for. Kibo knows, so Kibo is asked. The client and its
# helpers are the cancellation workflow's: one Kibo implementation in this
# repository, not two.
KIBO_IMPORT_ERROR = ""
try:
    from process_warehouse_cancellations import (  # noqa: E402
        KiboClient, kibo_credentials, load_tenant_map,
        shipment_status_of, shipment_order_number_of,
    )
    # tenant_and_site lives with the brand-to-site map, in the reconciler.
    from reconcile_cancellations import tenant_and_site  # noqa: E402
    KIBO_AVAILABLE = True
except Exception as exc:  # noqa: BLE001 - the rest of the run does not need Kibo
    KIBO_AVAILABLE = False
    # Kept so the log can say what went wrong rather than going quiet: a
    # swallowed ImportError here turns the whole Kibo stage into a no-op.
    KIBO_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"

# The one fulfilment status whose documents are worth asking for.
PREPARE_FOR_SHIPMENT = "prepare for shipment"
# A shipment Kibo calls FULFILLED has left; there is nothing left to fix.
FULFILLED_STATUS = "FULFILLED"
# Kibo moved the shipment to another location rather than fulfilling or
# cancelling it. The shipment number on this row is settled: whatever is left
# to do belongs to the shipment Kibo created in its place, which arrives on
# its own row. Nothing here is worth asking the warehouse about, so it is kept
# off the operational table and off the dashboard.
REASSIGNED_STATUS = "REASSIGNED"
# Kibo's spelling of a cancelled shipment — one L, against the warehouse
# event's CANCELLED, which has two. They are different systems and the
# resolve pass needs both, so the difference is kept visible here rather than
# normalised away.
CANCELLED_STATUS = "CANCELED"
# Every shipment this run found REASSIGNED. Mirrors FULFILLED_BY_KIBO: the
# resolve pass reads it at the end, once the rows have been written.
REASSIGNED_BY_KIBO: set = set()
# Every shipment this run found FULFILLED, from whichever stage found it. The
# resolve pass archives them at the end, by which point they have been
# written. A module-level set rather than a parameter through five stages:
# one run, one pass, nothing concurrent.
FULFILLED_BY_KIBO: set = set()


def live_status(row: Dict[str, str]) -> str:
    """
    The freshest fulfilment status known for a row.

    Kibo's answer where this run got one, and the first-seen status otherwise.
    Every decision that asks "what is this shipment doing now" reads this
    rather than either column directly, so a run where Kibo could not be
    reached falls back to what was already known instead of to a blank.
    """
    return (str(row.get("updated_fulfillment_status", "") or "").strip()
            or str(row.get("initial_fulfillment_status", "") or "").strip())


def fulfillment_status_of(shipment: Dict[str, Any]) -> str:
    """
    The fulfilment status of a Kibo shipment — what the OMS table calls
    `fulfillmentStatus` and the console calls the active task.

    Kibo does not send it as a field of its own on getShipments. It is the
    name of the workflow task that is currently active, so it is read from
    there: the explicit field first in case a response ever carries one, then
    activeTaskName, then the task list itself.
    """
    for key in ("fulfillmentStatus", "fulfillment_status"):
        value = str(shipment.get(key) or "").strip()
        if value:
            return value
    workflow = shipment.get("workflowState")
    if not isinstance(workflow, dict):
        return ""
    value = str(workflow.get("activeTaskName") or "").strip()
    if value:
        return value
    for task in workflow.get("taskList") or []:
        if isinstance(task, dict) and task.get("active"):
            return str(task.get("name") or "").strip()
    return ""


# Where the channel sits on a Kibo shipment. It is a property of the items,
# not of the shipment, and which list holds them depends on what has happened
# to the shipment: the live ones in items, and once it is cancelled or moved
# they are in canceledItems or reassignedItems instead. All three are read, in
# that order. The shipment's own data.channel is deliberately not read.
ITEM_LISTS = ("items", "canceledItems", "reassignedItems",
              "rejectedItems", "transferredItems", "substitutedItems")


def channel_of(shipment: Dict[str, Any]) -> str:
    """The sales channel of a Kibo shipment, from whichever list has items."""
    for name in ITEM_LISTS:
        entries = shipment.get(name)
        if not isinstance(entries, list):
            continue
        for item in entries:
            if not isinstance(item, dict):
                continue
            data = item.get("data")
            if isinstance(data, dict):
                value = str(data.get("channel") or "").strip()
                if value:
                    return value
    # No fallback to the shipment's own data.channel. On a marketplace order
    # that field carries the integration that brought the order in — "Order
    # Connector" — while the sales channel the business means is on the items
    # ("Farfetch"). A plausible wrong answer is worse than a blank the
    # backfill will try again.
    return ""


# The columns the backfill fills in, and where each one comes from. The
# monitoring table is asked first where it has an answer, and Kibo's
# getShipments is the fallback for every one of them — including the two that
# only the monitoring table can answer, which simply stay as they are when it
# does not.
BACKFILL_COLUMNS = (
    "oerodp",
    "location_name",
    # Not asked for by name, but oerodp is built from it, so a run that could
    # not fill this in could not fill in oerodp either. Kibo answers it.
    "logistic_order_number",
    "oms_shipment_number",
    "ko_source",
    "blocking_reason",
    "order_submit_date",
    "shipment_creation_date",
    "initial_fulfillment_status",
    "updated_fulfillment_status",
    "location_provider",
    "channel",
)

# Which warehouse a Kibo fulfilment location belongs to. Kibo names locations
# by warehouse and site — TMEXGX0058, TLITGX0058 — so the provider is read out
# of the code itself rather than from a list that would need a new entry for
# every site GXO opens.
LOCATION_PROVIDERS = (
    ("GX", GXO),
    ("MODA", MODA),
    ("MD", MODA),
)


def provider_from_location(code: Any) -> str:
    """The warehouse behind a Kibo fulfillmentLocationCode."""
    text = str(code or "").strip().upper()
    if not text:
        return ""
    for marker, provider in LOCATION_PROVIDERS:
        if marker in text:
            return provider
    return ""


def assigned_date_of(shipment: Dict[str, Any]) -> str:
    """
    When the shipment was assigned to its fulfilment location.

    Kibo has no field for it. The shipment record is created at the moment it
    is assigned to a location, so auditInfo.createDate is that moment, and the
    change messages confirm it: the shipment is created and its location set
    in the same second. acceptedDate is the warehouse accepting the work,
    which is later and not the same thing, so it is only the fallback.
    """
    audit = shipment.get("auditInfo")
    if isinstance(audit, dict):
        value = str(audit.get("createDate") or "").strip()
        if value:
            return value
    return str(shipment.get("acceptedDate") or "").strip()


def logistic_from_kibo(shipment: Dict[str, Any], shipment_order_number: str) -> str:
    """
    The logistics order number, rebuilt from Kibo's shipmentNumber.

    GXO's logisticOrderNumber is the two-letter brand prefix followed by Kibo's
    shipmentNumber: OW109712 is Kibo shipment 109712 at brand OW. The
    cancellation reconciler goes the other way, deriving the Kibo number by
    dropping the prefix; this is the same rule read backwards, which is what
    makes a shipment the warehouse told us about but never gave a logistics
    number for recoverable.

    It matters beyond the column itself: oerodp cannot be built without the
    logistics order number, so a GXO shipment that arrived through the
    workbook or the warehouse's own KO list and never carried one had no
    oerodp either.
    """
    number = str(shipment.get("shipmentNumber") or "").strip()
    brand = str(shipment_order_number or "").strip().upper()[:2]
    if not number or len(brand) < 2 or not brand.isalpha():
        return ""
    return f"{brand}{number}"


def kibo_details(shipment: Dict[str, Any],
                 shipment_order_number: str = "") -> Dict[str, str]:
    """
    Everything the backfill can read off one getShipments response.

    The fallback for all nine columns. Each is read on its own terms rather
    than from one blob, because they sit in different places: the status is a
    workflow task, the channel is a property of the items, the provider is
    encoded in the location code, and only the two numbers and the submit date
    are plain fields.
    """
    status = fulfillment_status_of(shipment)
    if not status:
        # Asked for explicitly: when there is no active task, the shipment's
        # own status is the best available answer — a READY shipment with no
        # task is still meaningfully READY.
        status = str(shipment.get("shipmentStatus") or "").strip()
    number = shipment.get("shipmentNumber")
    return {
        "oms_shipment_number": str(number).strip() if number not in (None, "") else "",
        "order_submit_date": str(shipment.get("orderSubmitDate") or "").strip(),
        "shipment_creation_date": assigned_date_of(shipment),
        # Both, from the one answer: the live status for updated_, and the
        # same value for initial_ so the backfill can fill a row that never
        # had one. The backfill only ever writes into blanks, so this cannot
        # overwrite a first-seen status that is already recorded.
        "updated_fulfillment_status": status,
        "initial_fulfillment_status": status,
        "location_provider": provider_from_location(
            shipment.get("fulfillmentLocationCode")
            or shipment.get("assignedLocationCode")),
        "location_name": str(shipment.get("fulfillmentLocationCode")
                             or shipment.get("assignedLocationCode") or "").strip(),
        "channel": channel_of(shipment),
        "logistic_order_number": logistic_from_kibo(shipment, shipment_order_number),
    }


def kibo_client_for(args: argparse.Namespace, log: Log):
    """The Kibo client, or None with a reason in the log."""
    if not KIBO_AVAILABLE:
        log(f"  The Kibo client could not be imported ({KIBO_IMPORT_ERROR}); "
            "fulfilment statuses are left as they are.")
        return None, {}
    try:
        client = KiboClient(credentials=kibo_credentials(), auth_url=args.kibo_auth_url,
                            host=args.kibo_host, timeout=args.timeout,
                            max_retries=args.max_retries)
    except Exception as exc:  # noqa: BLE001 - a missing secret is not fatal
        log(f"  Kibo is not reachable: {exc} Fulfilment statuses are left as they are.")
        return None, {}
    return client, load_tenant_map(args.tenant_map)


# Kibo's answer for each shipment, remembered for the run. A run can hold
# several batches — the pending CSV and GXO's own KO list arrive separately,
# and the same file can be uploaded twice — and they overlap heavily. Kibo's
# answer cannot change between two batches of one run, so it is asked once and
# every later row is served from here. Failures are remembered too: a lookup
# that failed is not worth repeating a minute later, and the row still gets
# the reason it failed.
_KIBO_FACTS: Dict[str, Dict[str, str]] = {}

# The same, for the documents and MODA label APIs. Keyed by shipment number,
# holding the whole outcome, so a repeat row is judged exactly as the first
# was — same verdict, same ko_fix_date, no second call.
_DOCUMENTS_RESULTS: Dict[str, Dict[str, Any]] = {}


def _kibo_failure(reason: str) -> Dict[str, str]:
    """A remembered Kibo failure: no facts, and why."""
    return {"status": "", "state": "", "channel": "", "logistic": "",
            "location": "", "error": reason}


# The monitoring table, read once per run and remembered. Two stages want it
# — the prefill below, before any call is made, and attach_monitoring after —
# and reading it twice would be the same query for the same answer.
_MONITORING_CACHE: Dict[str, Dict[str, str]] = {}
_MONITORING_ASKED: set = set()


def monitoring_for(rows: List[Dict[str, str]], args: argparse.Namespace,
                   log: Log, client: Any) -> Dict[str, Dict[str, str]]:
    """What the monitoring table says about these shipments, cached."""
    if client is None:
        return {}
    numbers = {normalise_shipment(row["shipment_order_number"]) for row in rows}
    fresh = sorted(numbers - _MONITORING_ASKED)
    if fresh:
        _MONITORING_CACHE.update(table_module.fetch_monitoring(client, fresh, log))
        _MONITORING_ASKED.update(fresh)
    return {number: _MONITORING_CACHE[number]
            for number in numbers if number in _MONITORING_CACHE}


def prefill_status(rows: List[Dict[str, str]], args: argparse.Namespace,
                   log: Log, client: Any) -> None:
    """
    Fill location_provider and initial_fulfillment_status before anything is
    called.

    Both decide whether a shipment is asked about at all — the provider picks
    the endpoint, the status decides whether the documents are worth asking
    for — and both used to be filled by attach_monitoring, which runs *after*
    the calls. A shipment whose row was blank at that moment was recorded as
    "not asked: no fulfillment_status" and then, a few lines later, given the
    very status that would have had it asked. It would wait a whole run for
    an answer the table already had.
    """
    if client is None:
        return
    found = monitoring_for(rows, args, log, client)
    if not found:
        return
    filled = {"provider": 0, "status": 0}
    for row in rows:
        entry = found.get(normalise_shipment(row["shipment_order_number"]), {})
        if not str(row.get("location_provider", "") or "").strip():
            value = str(entry.get("location_provider", "") or "").strip()
            if value:
                row["location_provider"] = value.upper()
                filled["provider"] += 1
        if not str(row.get("initial_fulfillment_status", "") or "").strip():
            value = str(entry.get("fulfillment_status", "") or "").strip()
            if value:
                row["initial_fulfillment_status"] = value
                filled["status"] += 1
    if filled["provider"] or filled["status"]:
        log(f"  From the monitoring table, before calling: "
            f"{filled['provider']} location_provider, "
            f"{filled['status']} initial_fulfillment_status filled in.")


def refresh_fulfillment(rows: List[Dict[str, str]], args: argparse.Namespace,
                        log: Log) -> set:
    """
    Ask Kibo what each GXO shipment is really waiting on, and act on it.

    Applies to every shipment in the run, at every warehouse, whatever status
    it is stored in. Kibo is the system of record for what a shipment is
    actually doing, and a stored status is only ever as fresh as the run that
    wrote it: a shipment sitting in "Validate Items In Stock" today may be
    ready tomorrow, and one that looks ready may have shipped an hour ago.
    Asking about all of them is what makes updated_fulfillment_status mean
    what it says.

    This used to be narrower — GXO only, and only where the stored status was
    not already "Prepare for Shipment". The per-run cache below is what makes
    the wider net affordable: each shipment is asked about once however many
    batches and passes want it.

    What each answer does:

      Prepare for Shipment   the status is written to the row, and
                             check_documents asks the documents API for it in
                             this same run.
      shipmentStatus FULFILLED
                             it has shipped. Marked fixed and returned in the
                             set this function hands back, which the resolve
                             pass archives.
      any other status       written to the row and logged. Nothing is asked
                             of the warehouse; the next run asks Kibo again.
      nothing                logged, and asked again next run.

    Returns the shipments Kibo calls FULFILLED, and adds them to
    FULFILLED_BY_KIBO for the resolve pass.
    """
    if args.dry_run:
        return set()
    # Every row, every run. The two reasons that used to narrow this — a GXO
    # shipment whose status might have moved, and any shipment with no channel
    # — are both still served by asking about all of them, and the live status
    # is wanted for its own sake now rather than only as a routing decision.
    wanted = list(rows)
    needs_status = wanted
    needs_channel = [row for row in rows
                     if not str(row.get("channel", "") or "").strip()]
    if not wanted:
        return set()

    client, tenant_map = kibo_client_for(args, log)
    if client is None:
        return set()

    already = sum(1 for row in wanted
                  if normalise_shipment(row["shipment_order_number"]) in _KIBO_FACTS)
    log(f"  Kibo: GET https://t<tenant>.{args.kibo_host}"
        "/api/commerce/shipments?filter=externalOrderId==<number> for "
        f"{len(wanted) - already} shipment(s) — {len(needs_status)} for a "
        f"fulfilment status, {len(needs_channel)} for a channel"
        + (f"; {already} already answered earlier in this run" if already else "")
        + ".")
    fulfilled: set = set()
    shown = False
    counts = {"channel": 0, "logistic": 0, "location": 0, "ready": 0,
              "fulfilled": 0, "reassigned": 0, "other": 0, "unknown": 0,
              "failed": 0, "reused": 0}
    for row in wanted:
        number = row["shipment_order_number"]
        key = normalise_shipment(number)
        remembered = _KIBO_FACTS.get(key)
        if remembered is not None:
            # Asked already, earlier in this run. The row is filled from the
            # answer rather than from a second identical call.
            counts["reused"] += 1
            if remembered["error"]:
                counts["failed"] += 1
                row["_ko_status"] = remembered["error"]
                continue
            status = remembered["status"]
            state = remembered["state"]
            channel = remembered["channel"]
            logistic = remembered["logistic"]
            location = remembered["location"]
        else:
            tenant, site, tenant_error = tenant_and_site(
                number, tenant_map, args.default_tenant_id, args.default_site_id)
            if tenant_error:
                counts["failed"] += 1
                reason = f"not asked: {tenant_error}"
                row["_ko_status"] = reason
                _KIBO_FACTS[key] = _kibo_failure(reason)
                continue
            try:
                shipments, _, error = client.get_shipments_for_order(tenant, site, number)
            except Exception as exc:  # noqa: BLE001 - one shipment, not the run
                counts["failed"] += 1
                reason = f"not asked: the Kibo lookup failed: {exc}"
                row["_ko_status"] = reason
                _KIBO_FACTS[key] = _kibo_failure(reason)
                continue
            if error:
                counts["failed"] += 1
                reason = f"not asked: the Kibo lookup failed: {error}"
                row["_ko_status"] = reason
                _KIBO_FACTS[key] = _kibo_failure(reason)
                continue

            if not shown:
                # The first lookup is shown in full, so the log says plainly that
                # Kibo was called and what came back. The rest are counted.
                shown = True
                log(f"    e.g. {number}: {len(shipments)} shipment(s) from tenant "
                    f"{tenant}" + (f", site {site}" if site else "")
                    + (f" — {fulfillment_status_of(shipments[0])!r}, "
                       f"{shipment_status_of(shipments[0])!r}" if shipments else ""))

            mine = [shipment for shipment in shipments
                    if normalise_shipment(shipment_order_number_of(shipment))
                    == normalise_shipment(number)] or shipments
            status = ""
            state = ""
            channel = ""
            logistic = ""
            location = ""
            for shipment in mine:
                status = status or fulfillment_status_of(shipment)
                state = state or str(shipment_status_of(shipment) or "").strip().upper()
                channel = channel or channel_of(shipment)
                logistic = logistic or logistic_from_kibo(shipment, number)
                location = location or str(shipment.get("fulfillmentLocationCode")
                                           or shipment.get("assignedLocationCode")
                                           or "").strip()
            _KIBO_FACTS[key] = {"status": status, "state": state,
                                "channel": channel, "logistic": logistic,
                                "location": location, "error": ""}
        if channel and not str(row.get("channel", "") or "").strip():
            row["channel"] = channel
            counts["channel"] += 1
        # The logistics order number while Kibo is on the line. A GXO shipment
        # that came in through the workbook or the warehouse's KO list often
        # has none, and without it oerodp cannot be built at all — which is why
        # a handful of GXO rows had an empty oerodp however often the run saw
        # them. Taken only when the row has none: GXO's own number wins.
        if logistic and not str(row.get("logistic_order_number", "") or "").strip():
            row["logistic_order_number"] = logistic
            counts["logistic"] += 1
        # The fulfilment location, parked rather than applied: this pass runs
        # before attach_monitoring, and the monitoring table has first say. It
        # is applied there, and only where nothing else answered.
        if location and not str(row.get("_kibo_location", "") or "").strip():
            row["_kibo_location"] = location
            counts["location"] += 1

        # No active task means no fulfilment status of its own, and then the
        # shipment's own status is the best answer there is: a FULFILLED or
        # CANCELED shipment with no live task is still meaningfully in that
        # state. Same rule as kibo_details, so the two cannot disagree about
        # what a shipment with no task is doing.
        recorded = status or state
        if recorded:
            row["updated_fulfillment_status"] = recorded
            # A shipment first seen by this run has no first-seen status yet;
            # Kibo's answer is the earliest anyone knows of it.
            if not str(row.get("initial_fulfillment_status", "") or "").strip():
                row["initial_fulfillment_status"] = recorded
        if state == REASSIGNED_STATUS:
            # Checked before the ready test on purpose: a reassigned shipment
            # can still carry a live "Prepare for Shipment" task, and asking
            # the warehouse for documents against a shipment Kibo has moved
            # would be asking about the wrong one. Not ko_fixed — nothing was
            # fixed — just settled, and resolved by the pass at the end.
            counts["reassigned"] += 1
            REASSIGNED_BY_KIBO.add(normalise_shipment(number))
            row["_ko_status"] = ("Kibo says REASSIGNED: the shipment moved to "
                                 "another location and is not this row's work")
        elif status.strip().lower() == PREPARE_FOR_SHIPMENT:
            counts["ready"] += 1
        elif state == FULFILLED_STATUS:
            counts["fulfilled"] += 1
            fulfilled.add(normalise_shipment(number))
            FULFILLED_BY_KIBO.add(normalise_shipment(number))
            # Fixed, and dated now: nothing is going to be asked of the
            # warehouse for a shipment that has already left.
            if not row.get("ko_fix_date"):
                row["ko_fix_date"] = iso(now_utc())
            row["ko_fixed"] = "yes"
            row["_ko_status"] = "Kibo says FULFILLED: the shipment has left"
        elif status:
            counts["other"] += 1
            row["_ko_status"] = (f"not asked: Kibo says '{status}', not "
                                 "'Prepare for Shipment'")
        else:
            counts["unknown"] += 1
            row["_ko_status"] = ("not asked: Kibo returned no fulfilment status "
                                 + (f"({state})" if state else "and no shipment"))
        if args.sleep:
            time.sleep(args.sleep)

    log("  Kibo: "
        + ", ".join(f"{count} {name}" for name, count in (
            ("ready for documents", counts["ready"]),
            ("FULFILLED", counts["fulfilled"]),
            ("REASSIGNED", counts["reassigned"]),
            ("in another status", counts["other"]),
            ("with no status", counts["unknown"]),
            ("that could not be read", counts["failed"]),
            ("answered from an earlier batch of this run",
             counts["reused"]),
            ("given a channel", counts["channel"]),
            ("given a logistics order number", counts["logistic"]),
            ("carrying a Kibo fulfilment location", counts["location"]),
        ) if count) + ".")
    return fulfilled


def endpoint_for(row: Dict[str, str]) -> str:
    """
    Which endpoint this shipment is asked about, or "" for none.

    Only the two warehouses this workflow can actually ask are called: MODA
    behind its own getlabel endpoint, GXO behind the logistics documents one.
    A shipment at any other warehouse — and one whose warehouse is not known —
    is not called at all. There is nobody to ask, and a call that cannot
    succeed still costs a round trip and still writes a verdict into the table
    as though it meant something.

    A blank location_provider falls back to `source`: a shipment that came
    from the GXO side of an upload is GXO's, whether or not the column has
    caught up. Anything else is left alone and the row says so.
    """
    provider = str(row.get("location_provider", "") or "").strip().upper()
    if provider == MODA:
        return "moda"
    if provider == GXO:
        return "documents"
    if not provider and GXO in normalise_source(row.get("source", "")).upper():
        return "documents"
    return ""


def check_documents(rows: List[Dict[str, str]], args: argparse.Namespace, log: Log) -> None:
    """Ask the documents API about every shipment not already fixed."""
    if args.dry_run:
        log("  Dry run: the documents API is not called and ko_fix_date stays empty.")
        return
    to_check = [row for row in rows if row["ko_fixed"] != "yes"]
    if not to_check:
        log("  Nothing to ask the documents API about.")
        return

    if len(to_check) != len(rows):
        log(f"  {len(rows) - len(to_check)} already fixed, {len(to_check)} to check.")

    # Only MODA and GXO are asked. Everything else is recorded and left.
    routed = {id(row): endpoint_for(row) for row in to_check}
    moda_rows = [row for row in to_check if routed[id(row)] == "moda"]
    main_rows = [row for row in to_check if routed[id(row)] == "documents"]
    skipped = [row for row in to_check if not routed[id(row)]]
    moda_ids = {id(row) for row in moda_rows}

    if skipped:
        providers = sorted({str(row.get("location_provider", "") or "").strip().upper()
                            or "(not known)" for row in skipped})
        log(f"  {len(skipped)} shipment(s) are at neither GXO nor MODA "
            f"({', '.join(providers)}); they are recorded, not called.")
        for row in skipped:
            provider = str(row.get("location_provider", "") or "").strip().upper()
            row["_ko_status"] = (
                f"not checked: the warehouse is {provider}, which this workflow "
                "cannot ask" if provider else
                "not checked: the warehouse is not known, so there is nobody to ask")
        to_check = [row for row in to_check if routed[id(row)]]
        if not to_check:
            log("  Nothing left to ask about.")
            return

    # Of those, only the ones waiting to be prepared are worth asking about.
    # The rest are recorded with the state that kept them out and left alone:
    # they keep their row, they keep ko_fixed = false, and no call is made.
    #
    # Both warehouses, on the same rule. This used to read main_rows alone, so
    # a MODA shipment had its label asked for in any status at all while a GXO
    # one in the same status was held back. The status comes from Kibo, which
    # knows nothing of which warehouse the shipment is in, so there was no
    # reason for the two to be judged differently.
    not_ready = [row for row in to_check
                 if live_status(row).lower() != DOCUMENTS_STATUS]
    if not_ready:
        for row in not_ready:
            state = live_status(row)
            row["_ko_status"] = (
                f"not asked: fulfilment status is {state}, not "
                f"'{DOCUMENTS_STATUS.title()}'" if state else
                "not asked: no fulfilment status")
        skipped_ids = {id(row) for row in not_ready}
        main_rows = [row for row in main_rows if id(row) not in skipped_ids]
        moda_rows = [row for row in moda_rows if id(row) not in skipped_ids]
        moda_ids = {id(row) for row in moda_rows}
        to_check = [row for row in to_check if id(row) not in skipped_ids]
        states = sorted({live_status(row) or "(none)" for row in not_ready})
        log(f"  {len(not_ready)} shipment(s) not asked, "
            f"fulfilment status is {', '.join(states[:6])}"
            + (" and others" if len(states) > 6 else "") + ".")
    if not to_check:
        log("  Nothing left to ask about.")
        return

    session = make_session(args.max_retries)
    token = ""
    if main_rows:
        token = get_access_token(session, args.auth_url)
        log(f"  Authenticated against {args.auth_url}")
        log(f"  {len(main_rows)} shipment(s) in '{DOCUMENTS_STATUS.title()}' "
            f"go to {args.url}")
    moda_key = ""
    if moda_rows:
        try:
            moda_key = moda_token()
            log(f"  {len(moda_rows)} MODA shipment(s) go to {args.moda_url}")
        except RuntimeError as exc:
            # One missing secret should cost the MODA shipments, not the run.
            log(f"  {exc} The {len(moda_rows)} MODA shipment(s) are not checked.")
            for row in moda_rows:
                row["_ko_status"] = "MODA label not checked: no credentials"
            moda_rows = []
            moda_ids = set()
            to_check = main_rows
    fixed_count = 0
    reused_count = 0
    sampled = False
    for position, row in enumerate(to_check, start=1):
        key = normalise_shipment(row["shipment_order_number"])
        outcome = _DOCUMENTS_RESULTS.get(key)
        fresh = outcome is None
        if fresh:
            if id(row) in moda_ids:
                outcome = call_moda_label(
                    session, moda_key, args.moda_url,
                    row["shipment_order_number"], args.zpl_check,
                )
            else:
                outcome = call_documents_api(
                    session, token, args.url, row["shipment_order_number"],
                    row["logistic_order_number"], args.zpl_check,
                    row.get("_weight", ""), row.get("_parcels", ""),
                )
            _DOCUMENTS_RESULTS[key] = outcome
        else:
            # Asked already, earlier in this run. The verdict is applied
            # again — same answer, same ko_fix_date — without a second call.
            reused_count += 1
        flags = outcome["flags"]
        if not sampled and fresh:
            # One call shown in full per run: what was sent, and what came
            # back. Field names are where this has gone wrong before, and a
            # log that shows them settles it without a Postman session.
            sampled = True
            log(f"    e.g. sent     {json.dumps(outcome.get('request', {}))[:200]}")
            log(f"    e.g. received HTTP {outcome['http_status']}, top-level keys: "
                + ", ".join(outcome.get("response_keys", [])) or "(none)")
        row["api_call_date"] = iso(outcome["called_at"])
        row["eligible_for_package_closure"] = yes_no(flags["eligible"])
        row["shipping_tracking_number"] = flags["shipping_tracking"]
        row["return_tracking_number"] = flags["return_tracking"]
        # The per-label and per-document flags are not stored. judge() still
        # reads every one of them to decide ko_fixed, and _ko_status still
        # names the ones that carried the verdict, so the reasoning survives
        # in ko_reason without seven columns of yes/no beside it.
        row["_ko_status"] = outcome["why"]
        if outcome["fixed"]:
            row["ko_fix_date"] = iso(outcome["called_at"])
            row["ko_fixed"] = "yes"
            fixed_count += 1
        if position % 25 == 0 or position == len(to_check):
            log(f"  {position}/{len(to_check)} checked, {fixed_count} newly fixed"
                + (f", {reused_count} from an earlier batch of this run"
                   if reused_count else "") + ".")

    # Why the rest are not fixed, grouped. A run that fixes nothing should say
    # what the API actually answered rather than leaving it to be queried for.
    unfixed: Dict[str, int] = {}
    for row in to_check:
        if row["ko_fixed"] != "yes":
            reason = str(row.get("_ko_status", "") or "no reason recorded")
            unfixed[reason] = unfixed.get(reason, 0) + 1
    for reason, count in sorted(unfixed.items(), key=lambda item: -item[1])[:8]:
        log(f"    {count:>4} × {reason[:110]}")
        if args.sleep > 0:
            time.sleep(args.sleep)
    log(f"  {fixed_count} of {len(to_check)} checked shipment(s) were fixed; "
        f"{sum(1 for row in rows if row['ko_fixed'] == 'yes')} of {len(rows)} "
        "now carry a ko_fix_date."
        + (f" {reused_count} of them were answered from an earlier batch of "
           "this run rather than called again." if reused_count else ""))


def tried_after_fix(ko_fix_date: str, last_tried: str) -> bool:
    """
    Did the warehouse try to print after the KO was recorded as fixed?

    The question the column answers is whether the fix reached GXO: a shipment
    fixed days ago that the warehouse has not touched since is fixed only on
    paper. Both values are the ISO timestamps this worker writes, so an unset
    one — no fix yet, or no attempt on record — is simply false.
    """
    if not ko_fix_date or not last_tried:
        return False
    try:
        fixed_at = datetime.fromisoformat(ko_fix_date.replace("Z", "+00:00"))
        tried_at = datetime.fromisoformat(last_tried.replace("Z", "+00:00"))
    except ValueError:
        return False
    return tried_at > fixed_at


def attach_gxo_attempts(rows: List[Dict[str, str]], args: argparse.Namespace,
                        log: Log, client: Any) -> None:
    """last_tried_by_gxo_date, get_documents_error and the logistics order."""
    if client is None:
        log("  --no-bigquery: last_tried_by_gxo_date stays empty.")
        return
    if client is None:
        log("  No BigQuery client: last_tried_by_gxo_date stays empty.")
        return
    keys = [
        (normalise_shipment(row["shipment_order_number"]),
         normalise_shipment(row["logistic_order_number"]))
        for row in rows
    ]
    attempts = fetch_gxo_attempts(client, keys, args.bq_lookback_days, log)
    for row in rows:
        values = gxo_columns(attempts.get(normalise_shipment(row["shipment_order_number"])))
        row["last_tried_by_gxo_date"] = values["last_tried"]
        row["gxo_result_code"] = values["result_code"]
        # Kept aside rather than written straight to ko_reason: the monitoring
        # table has three fields that outrank it, and attach_monitoring below
        # decides between them.
        row["_documents_error"] = values["error"]
        if not row["logistic_order_number"] and values["logistic"]:
            row["logistic_order_number"] = values["logistic"]
        # Once true this stays true in the table; the merge ORs it with what is
        # already there, so a later run cannot unsay it.
        if tried_after_fix(row["ko_fix_date"], row["last_tried_by_gxo_date"]):
            row["tried_by_gxo_after_the_fix"] = "yes"
    log(f"  {sum(1 for r in rows if r['last_tried_by_gxo_date'])} shipment(s) "
        "have a warehouse attempt on record, "
        f"{sum(1 for r in rows if r['tried_by_gxo_after_the_fix'] == 'yes')} of them "
        "after the fix.")


def run_resolve(client: Any, wlfs_client: Any, gxo_present: Optional[set],
                log: Log, run: str) -> int:
    """
    Decide which shipments are resolved, and write them.

    Resolved means archived: the row is copied to the history table and
    removed from the operational one. That is not undoable by a later run, so
    the rules are deliberately narrow — a shipment is resolved only when its
    own source has stopped reporting it.

      GXO, to cancel
             prd_shipmentStatus has a CANCELLED event for the shipment number
             and the logistics order number together. Nothing else archives
             one of these — not the workbook, not gxo_ko. We asked GXO to
             cancel the shipment, and the cancellation event is the warehouse
             saying it did; dropping off a listing is not an answer to that.

      GXO, the rest
             prd_shipmentStatus has a SHIPPED event for the pair — the
             shipment left the warehouse, which settles it whatever any
             listing says — or the run carried a workbook and the shipment is
             in neither that workbook nor the latest gxo_ko import. A GXO
             shipment in either of them is never archived for absence, and on
             a run with no workbook nothing is archived for absence at all:
             absence from something that was not read is not absence.

      any    Kibo called it FULFILLED during this run — it has left the
             warehouse, which settles it whatever anything else says.

      any    Kibo calls it CANCELED and prd_shipmentStatus has a CANCELLED
             event for the same pair. Cancelled on both sides, so there is
             nothing to chase; either half on its own is not enough.

      other  the shipment is no longer in the filtered view of the monitoring
             table — the same filters the intake applies. Once that table has
             stopped calling it blocked there is nothing left to do with it.

    `gxo_present` is the shipments the workbook and gxo_ko carry, or None on a
    run that read neither.
    """
    if client is None:
        log("  No BigQuery client: nothing is resolved.")
        return 0
    rows = table_module.fetch_unresolved(client, log)
    if not rows:
        return 0

    gxo_all = [row for row in rows if GXO in provider_of(row)]
    gxo_ids = {id(row) for row in gxo_all}
    other_rows = [row for row in rows if id(row) not in gxo_ids]

    # The shipments GXO was asked to cancel answer to the cancellation event
    # and to nothing else.
    to_cancel = [row for row in gxo_all if awaiting_cancellation(row)]
    cancel_ids = {id(row) for row in to_cancel}
    gxo_rows = [row for row in gxo_all if id(row) not in cancel_ids]

    # The filtered monitoring table, read once, and only if a non-GXO shipment
    # actually needs it. None means it could not be read, and then nothing is
    # resolved against it — far better than archiving everything.
    monitoring_open: Optional[set] = None
    if other_rows:
        monitoring_open = table_module.fetch_monitoring_open(client, log)

    # Every shipment Kibo calls CANCELED, at any warehouse. These are asked
    # about even when they are not GXO's: a shipment cancelled in Kibo and
    # confirmed cancelled by the warehouse is settled on both sides, and the
    # rule below archives it wherever it sits.
    cancelled_rows = [row for row in rows
                      if live_status(row).upper() == CANCELLED_STATUS]

    # prd_shipmentStatus, read once: the CANCELLED events settle the ones GXO
    # was asked to cancel and the ones Kibo has cancelled, the SHIPPED events
    # settle the rest. None means the table could not be read at all, and then
    # neither applies — not knowing is not the same as nothing having happened.
    event_rows = gxo_all + [row for row in cancelled_rows if id(row) not in gxo_ids]
    events: Optional[Dict[str, Any]] = None
    if event_rows:
        events = table_module.fetch_status_events(
            wlfs_client,
            [(normalise_shipment(row["shipment_order_number"]),
              normalise_shipment(row["logistic_order_number"])) for row in event_rows],
            log,
        )

    resolved: List[str] = []
    reasons = {"cancelled by GXO": 0, "shipped": 0, "fulfilled in Kibo": 0,
               "reassigned in Kibo": 0,
               "cancelled in Kibo and at the warehouse": 0,
               "gone from the GXO inputs": 0, "gone from the monitoring table": 0}

    # Kibo called these FULFILLED earlier in this run. They were written a
    # moment ago, so they are in the table and can be archived now.
    open_now = {normalise_shipment(row["shipment_order_number"]) for row in rows}
    for shipment in sorted(FULFILLED_BY_KIBO & open_now):
        resolved.append(shipment)
        reasons["fulfilled in Kibo"] += 1
    # And these Kibo moved elsewhere. Settled for the same reason and by the
    # same route: the work, if any is left, is on the shipment Kibo created in
    # this one's place.
    for shipment in sorted(REASSIGNED_BY_KIBO & open_now - set(resolved)):
        resolved.append(shipment)
        reasons["reassigned in Kibo"] += 1
    settled = set(resolved)

    if events is None:
        if gxo_all:
            log(f"  {SHIPMENT_EVENTS} could not be read, so none of the "
                f"{len(gxo_all)} GXO shipment(s) are archived on an event.")
    else:
        for row in to_cancel:
            shipment = normalise_shipment(row["shipment_order_number"])
            logistic = normalise_shipment(row["logistic_order_number"])
            if shipment in settled:
                continue
            if table_module.has_event(events, "cancelled", shipment, logistic):
                resolved.append(shipment)
                reasons["cancelled by GXO"] += 1
        waiting = len(to_cancel) - reasons["cancelled by GXO"]
        if waiting:
            log(f"  {waiting} shipment(s) GXO was asked to cancel have no CANCELLED "
                "event yet; they stay open whatever the inputs say.")

    # Cancelled on both sides: Kibo's shipmentStatus is CANCELED and
    # prd_shipmentStatus carries a CANCELLED event for the same pair. Nothing
    # is waiting on such a shipment anywhere, so it is archived instead of
    # being counted on the dashboard.
    #
    # Both halves are required, and for different reasons. A Kibo
    # cancellation the warehouse has not confirmed is still open work — that
    # is the whole point of the rule above this one. A CANCELLED event for a
    # shipment Kibo has not cancelled is somebody else's cancellation, not
    # this one's.
    if events is not None and cancelled_rows:
        done = set(resolved)
        for row in cancelled_rows:
            shipment = normalise_shipment(row["shipment_order_number"])
            if shipment in done:
                continue
            if table_module.has_event(events, "cancelled", shipment,
                                      normalise_shipment(row["logistic_order_number"])):
                resolved.append(shipment)
                done.add(shipment)
                settled.add(shipment)
                reasons["cancelled in Kibo and at the warehouse"] += 1
        still_open = len(cancelled_rows) - reasons["cancelled in Kibo and at the warehouse"]
        if still_open:
            log(f"  {still_open} shipment(s) Kibo calls {CANCELLED_STATUS} have no "
                "CANCELLED event at the warehouse yet; they stay open.")

    shipped_ids = set()
    if events is not None:
        for row in gxo_rows:
            shipment = normalise_shipment(row["shipment_order_number"])
            logistic = normalise_shipment(row["logistic_order_number"])
            if shipment in settled:
                continue
            if table_module.has_event(events, "shipped", shipment, logistic):
                resolved.append(shipment)
                reasons["shipped"] += 1
                shipped_ids.add(id(row))

    if gxo_present is None:
        if gxo_rows:
            log(f"  No workbook this run, so no GXO shipment is archived for "
                "absence.")
    else:
        for row in gxo_rows:
            if id(row) in shipped_ids:
                continue
            shipment = normalise_shipment(row["shipment_order_number"])
            if shipment in settled:
                continue
            if shipment not in gxo_present:
                resolved.append(shipment)
                reasons["gone from the GXO inputs"] += 1

    if monitoring_open is None:
        if other_rows:
            log(f"  The monitoring table could not be read, so none of the "
                f"{len(other_rows)} non-GXO shipment(s) are archived.")
    else:
        for row in other_rows:
            shipment = normalise_shipment(row["shipment_order_number"])
            if shipment in settled:
                continue
            if shipment not in monitoring_open:
                resolved.append(shipment)
                reasons["gone from the monitoring table"] += 1

    if not resolved:
        log("  Nothing to resolve.")
        return 0
    written, error = table_module.resolve_many(client, resolved, run, log)
    if error:
        return 0
    log(f"  {written} shipment(s) resolved — "
        + ", ".join(f"{count} {why}" for why, count in reasons.items() if count) + ".")
    return written


def awaiting_cancellation(row: Dict[str, str]) -> bool:
    """
    Is this a shipment GXO has been asked to cancel?

    Read from the action rather than the blocking reason, so the two cannot
    disagree: the action column is what the console shows the warehouse, and
    it is what this is waiting on an answer to.
    """
    return str(row.get("action_needed_from_gxo", "") or "").strip() == ACTION_CANCEL


def provider_of(row: Dict[str, str]) -> str:
    """
    GXO, MODA or "" — the warehouse this row belongs to.

    `source` is the fallback when location_provider is blank, for the same
    reason it is everywhere else: a shipment from the GXO side of an upload is
    GXO's whether or not the column has caught up. Getting this wrong in the
    GXO direction is the safe way round — a shipment wrongly called GXO is
    archived less often, not more.
    """
    provider = str(row.get("location_provider", "") or "").strip().upper()
    if provider:
        return provider
    source = normalise_source(row.get("source", "")).upper()
    return GXO if GXO in source else ""


# Blocking reasons that carry an action for the warehouse, and what it is.
# The first match wins, so the specific reason outranks the general rule that
# a fixed KO is a KO whose documents are waiting to be printed.
SHIPMENT_EVENTS = "tlg-wlfs-prd.til.prd_shipmentStatus"
CANCEL_ON_KIBO = "Shipment cancelled on Kibo but still pending for GXO"
ACTION_CANCEL = "To cancel the shipment"
ACTION_PRINT = "To print shipping documents"
# A reason of this shape names another shipment that is holding the stock.
# Nothing can be asked of the warehouse until that one is dealt with, so these
# are kept out of the console's working list.
STOCK_RESERVED = "Stock still reserved to the cancelled shipment"


# What `source` used to say, before it named the systems rather than the file
# formats. A row written by the old worker still carries one of these, and
# nothing recomputes `source` for a shipment that is not in a new upload, so
# the translation is applied to every stored row this worker touches. That
# makes a recheck or a backfill enough to fix the table, with or without
# sql/finish_ko_operational_rename.sql.
OLD_SOURCES = {
    "csv + excel": "TLG + GXO",
    "csv only": "TLG only",
    "excel only": "GXO only",
    "excel": "GXO only",
    "csv": "TLG only",
}


def normalise_source(value: str) -> str:
    """`source` as the systems behind the files, whichever generation wrote it."""
    text = str(value or "").strip()
    return OLD_SOURCES.get(text.lower(), text)


def is_gxo(row: Dict[str, str]) -> bool:
    """
    Whether this row is GXO's, by provider or, failing that, by source.

    The same test gxo_action makes: location_provider when it is filled in,
    and the source as the fallback for the rows written before that column
    existed. One helper so the action and the OERODP can never disagree about
    whose shipment it is.
    """
    known = str(row.get("location_provider", "") or "").strip().upper()
    if known == MODA:
        return False
    if known == GXO:
        return True
    return GXO in normalise_source(row.get("source", "")).upper()


def gxo_action(provider: str, blocking_reason: str, ko_fixed: str,
               source: str = "", awaiting_cancellation: bool = False) -> str:
    """
    What GXO has to do about this shipment, or nothing.

    Only ever filled for a GXO shipment: it is the warehouse's own worklist,
    and an action against a MODA shipment would be read as something to chase
    GXO about. The cancellation comes first — a shipment Kibo has cancelled is
    to be cancelled whether or not its documents would now print.

    `awaiting_cancellation` is the cancellation ledger's answer: the warehouse
    was sent this cancellation and has not confirmed it. It stands beside the
    blocking reason rather than behind it, because the two go out of step —
    a shipment can stop being reported as 'cancelled on Kibo but still pending
    for GXO' while GXO still owes an answer, and the action has to outlive the
    reason that raised it.

    `source` is the fallback for a row whose location_provider was never
    filled — the column arrived after those rows were written, and a shipment
    that came from the GXO side of an upload is GXO's whether or not anything
    has got round to saying so in a column of its own.
    """
    known = str(provider or "").strip().upper()
    if known == MODA:
        return ""
    if known != GXO and GXO not in normalise_source(source).upper():
        return ""
    if awaiting_cancellation or str(blocking_reason or "").strip() == CANCEL_ON_KIBO:
        return ACTION_CANCEL
    if str(ko_fixed or "").strip().lower() in ("yes", "true", "1"):
        return ACTION_PRINT
    return ""


def fill_oerodp(rows: List[Dict[str, str]], log: Log) -> None:
    """
    Build the OERODP for every GXO row, the one way.

        brandCode + the channel's first letter + '-' + logisticOrderNumber

    It used to be taken from the workbook when the workbook had it and built
    only for the rows it did not cover, and built with an empty channel at
    that — so a shipment's OERODP depended on which door it came in through,
    and the rows built without a channel were missing their letter. Now it is
    computed from the three columns for every GXO row, so two shipments with
    the same brand, channel and logistics order always read the same.

    A row keeps whatever it had when the three parts are not all there: an
    OERODP from the workbook is worth more than a blank, and half a code is
    worth less than either. The channel is the part most often missing, which
    is what the channel backfill is for.
    """
    built = changed = kept = 0
    for row in rows:
        if not is_gxo(row):
            continue
        candidate = build_oerodp(row.get("shipment_order_number", ""),
                                 row.get("channel", ""),
                                 row.get("logistic_order_number", ""))
        current = str(row.get("oerodp", "") or "").strip()
        if not candidate:
            kept += bool(current)
            continue
        built += 1
        if current and current != candidate:
            changed += 1
        row["oerodp"] = candidate

    if built or kept:
        message = f"  oerodp: {built} built from brand, channel and logistics order"
        if changed:
            message += f" ({changed} differ from what was stored)"
        if kept:
            message += f", {kept} left as they were for want of one of the three"
        log(message + ".")


def attach_monitoring(rows: List[Dict[str, str]], args: argparse.Namespace,
                      log: Log, client: Any) -> None:
    """
    ko_reason, and location_provider where the files could not say.

    ko_reason takes the first of these that has anything to say, which is the
    order they were asked for — the monitoring table's own account of the
    problem first, the documents API's error next, and the pending-monitoring
    action last, as the least specific of the five:

        external_error_message
        error_message
        blocking_reason
        the documents API's error
        action, from the CSV

    location_provider is only filled in here when neither file could answer:
    the workbook and the CSV's WMS column come first, because they say where
    the shipment is now rather than where the monitoring table last saw it.
    """
    if client is None:
        log("  No monitoring lookup: ko_reason falls back to what this run found.")
        found: Dict[str, Dict[str, str]] = {}
    else:
        found = monitoring_for(rows, args, log, client)

    # Who GXO still owes a cancellation to. None means the ledger could not be
    # read, and an unreadable ledger must not look like an empty one: a row
    # keeps whatever action it already had rather than having it cleared on
    # the strength of a lookup that did not happen.
    awaiting: Optional[set] = None
    if client is not None and table_module is not None:
        awaiting = table_module.fetch_awaiting_cancellation(client, log)

    from_monitoring = 0
    for row in rows:
        # A stored row can carry the old file-format source; the systems are
        # what every rule below reads, so it is translated first.
        row["source"] = normalise_source(row.get("source", ""))
        entry = found.get(normalise_shipment(row["shipment_order_number"]), {})
        reason = ""
        for field in ("external_error_message", "error_message", "blocking_reason"):
            value = str(entry.get(field, "") or "").strip()
            if value:
                reason = value
                from_monitoring += 1
                break
        if not reason:
            reason = str(row.get("_documents_error", "") or "").strip()
        if not reason:
            reason = str(row.get("action", "") or "").strip()
        row["ko_reason"] = reason

        if not row.get("location_provider"):
            row["location_provider"] = str(entry.get("location_provider", "") or "").strip()
        # The warehouse, in the order the three sources deserve: the
        # monitoring table has first say and is taken whatever format it
        # holds — code or description — then Kibo's fulfillmentLocationCode
        # where the table is null, and the CSV's "Location name" only when
        # neither spoke. The CSV used to win outright, which is why this
        # column filled up with warehouse descriptions ("TLG Italian
        # Warehouse GXO - WF") on rows where the table had the location code
        # the dashboard is keyed on.
        # refresh_fulfillment parked Kibo's code rather than applying it, so
        # that this order holds however the run reached here.
        location = (
            str(entry.get("location_name", "") or "").strip()
            or str(row.get("_kibo_location", "") or "").strip()
            or str(row.get("location_name", "") or "").strip()
        )
        if location:
            row["location_name"] = location
        # The channel only comes from the CSV, so a shipment the workbook or
        # gxo_ko brought in has none until this lookup fills it.
        if not row.get("channel"):
            row["channel"] = str(entry.get("channel", "") or "").strip()
        # Both are taken as the monitoring table has them, overwritten on
        # every run: they describe the shipment now, not when it was first
        # seen, and the table is the authority on both.
        if entry.get("ko_source"):
            row["ko_source"] = str(entry["ko_source"]).strip()
        if entry.get("blocking_reason"):
            row["blocking_reason"] = str(entry["blocking_reason"]).strip()
        pending_cancel = (awaiting is not None
                          and normalise_shipment(row["shipment_order_number"]) in awaiting)
        action = gxo_action(
            row.get("location_provider", ""), row.get("blocking_reason", ""),
            row.get("ko_fixed", ""), row.get("source", ""),
            awaiting_cancellation=pending_cancel)
        # An unreadable ledger leaves the stored action alone rather than
        # blanking it; everything else is recomputed from scratch each run.
        if action or awaiting is not None:
            row["action_needed_from_gxo"] = action

    cancels = sum(1 for r in rows if r.get("action_needed_from_gxo") == ACTION_CANCEL)
    prints = sum(1 for r in rows if r.get("action_needed_from_gxo") == ACTION_PRINT)
    log(f"  action_needed_from_gxo: {cancels} to cancel, {prints} to print.")
    log(f"  ko_reason: {sum(1 for r in rows if r['ko_reason'])} of {len(rows)} filled, "
        f"{from_monitoring} from the monitoring table.")
    log(f"  location_provider: {sum(1 for r in rows if r['location_provider'])} of "
        f"{len(rows)} known.")
    # Last, because it reads channel and location_provider, which the loop
    # above has only just filled in.
    fill_oerodp(rows, log)


def backfill_from_kibo(rows: List[Dict[str, str]], args: argparse.Namespace,
                       log: Log) -> Dict[str, int]:
    """
    Ask Kibo for every row that still has a gap, and fill only the gaps.

    The fallback behind the monitoring table, for all nine columns. A row is
    asked about when any of them is still blank after the monitoring pass, and
    the answer is written only into the blanks: a value the monitoring table
    gave is not second-guessed by Kibo, because the monitoring table is the
    business's own view of the shipment and Kibo is the system of record for
    what the warehouse is doing with it. The two disagree often enough that
    which one wins has to be decided once, here, and not per column.
    """
    counts = {name: 0 for name in BACKFILL_COLUMNS}
    counts["asked"] = counts["answered"] = counts["failed"] = 0

    wanted = [row for row in rows
              if any(not str(row.get(column, "") or "").strip()
                     for column in BACKFILL_COLUMNS)]
    if not wanted:
        log("  Every row is already complete; Kibo was not called.")
        return counts
    if args.dry_run:
        log(f"  --dry-run: {len(wanted)} row(s) would be asked about.")
        return counts

    client, tenant_map = kibo_client_for(args, log)
    if client is None:
        return counts

    log(f"  Kibo: {len(wanted)} row(s) have at least one gap; asking "
        f"getShipments for each.")
    shown = False
    for position, row in enumerate(wanted, start=1):
        number = row["shipment_order_number"]
        tenant, site, tenant_error = tenant_and_site(
            number, tenant_map, args.default_tenant_id, args.default_site_id)
        if tenant_error:
            counts["failed"] += 1
            continue
        counts["asked"] += 1
        try:
            shipments, _, error = client.get_shipments_for_order(tenant, site, number)
        except Exception as exc:  # noqa: BLE001 - one shipment, not the run
            counts["failed"] += 1
            log(f"    {number}: the Kibo lookup failed: {exc}")
            continue
        if error or not shipments:
            counts["failed"] += 1
            continue

        mine = [shipment for shipment in shipments
                if normalise_shipment(shipment_order_number_of(shipment))
                == normalise_shipment(number)] or shipments
        details: Dict[str, str] = {}
        for shipment in mine:
            for column, value in kibo_details(shipment, number).items():
                if value and not details.get(column):
                    details[column] = value
        if not details:
            continue
        counts["answered"] += 1

        filled = []
        for column, value in details.items():
            if value and not str(row.get(column, "") or "").strip():
                row[column] = value
                counts[column] += 1
                filled.append(column)
        if filled and not shown:
            shown = True
            log(f"    e.g. {number}: {', '.join(filled)}")
        if args.sleep:
            time.sleep(args.sleep)
        if position % 100 == 0:
            log(f"    {position}/{len(wanted)} asked.")

    return counts


def backfill_all(args: argparse.Namespace, log: Log, client: Any,
                 run: str) -> Dict[str, Any]:
    """
    Fill in the descriptive columns of both tables, and decide nothing.

    Nine columns, over the operational table and the archive: oerodp,
    oms_shipment_number, ko_source, blocking_reason, order_submit_date,
    shipment_creation_date, fulfillment_status, location_provider and channel.
    The monitoring table answers first where it can, Kibo's getShipments is
    the fallback for all of them, and oerodp is computed from brand, channel
    and logistics order once the other two are in.

    It calls no documents API, changes no verdict, resolves nothing and
    archives nothing. Rows are written back with update_columns rather than
    the merge, so a row the table does not have is never inserted — which is
    what makes it safe to run over the archive.
    """
    outcome: Dict[str, Any] = {"operational": {}, "archive": {}}

    for label, fetch, table in (
        ("the operational table", table_module.fetch_all, table_module.TABLE),
        ("the archive", table_module.fetch_all_history, table_module.HISTORY_TABLE),
    ):
        log("")
        log(f"Backfill: {label}.")
        stored = fetch(client, log, args.limit)
        if not stored:
            log("  Nothing to fill in.")
            continue

        rows: List[Dict[str, str]] = []
        for entry in stored:
            row = {column: entry.get(column, "") for column in OUTPUT_COLUMNS}
            row["_ko_status"] = ""
            row["_documents_error"] = ""
            rows.append(row)

        before = {column: sum(1 for r in rows if str(r.get(column, "") or "").strip())
                  for column in BACKFILL_COLUMNS}

        # 1. the monitoring table, which also fills ko_reason and the action,
        #    and ends by rebuilding oerodp from brand, channel and logistics
        #    order. 2. Kibo for whatever it left blank. 3. oerodp again,
        #    because Kibo may have just supplied the channel it needed.
        attach_monitoring(rows, args, log, client)
        kibo = backfill_from_kibo(rows, args, log)
        fill_oerodp(rows, log)

        for column in BACKFILL_COLUMNS:
            now = sum(1 for r in rows if str(r.get(column, "") or "").strip())
            gained = now - before[column]
            from_kibo = kibo.get(column, 0)
            log(f"  {column}: {before[column]} already set, {gained} newly filled"
                + (f" ({from_kibo} of them from Kibo)" if from_kibo else "")
                + f", {len(rows) - now} still blank.")

        written = table_module.update_columns(
            client, rows, BACKFILL_COLUMNS, run, log,
            table=table, dry_run=args.dry_run,
        )
        outcome["operational" if table == table_module.TABLE else "archive"] = {
            "rows": len(rows),
            "updated": written.get("updated", 0),
            "error": written.get("error", ""),
            "asked_kibo": kibo.get("asked", 0),
        }

    return outcome


def recheck_open(args: argparse.Namespace, log: Log, client: Any,
                 run: str, skip: Sequence[str] = ()) -> Tuple[List[Dict[str, str]],
                                                              Dict[str, Any]]:
    """
    Re-check what the table still holds open. Runs whatever the uploads say.

    Every shipment with ko_resolved = false is asked again, fixed ones
    included. A KO is fixed by the warehouse at its own pace and the only way
    to find out is to ask; and the live fulfilment status is wanted for every
    shipment on its own account, not only for the ones still being chased. A
    shipment already covered by this run's batches is left out — it has just
    been asked about, and `skip` carries those numbers.

    Nothing is resolved here: ko_resolved says a shipment has dropped out of
    the uploads, and this pass has no grounds to say that of anything.
    """
    already = {normalise_shipment(number) for number in skip}
    stored = table_module.fetch_unresolved(client, log)
    if not stored:
        return [], {}

    rows: List[Dict[str, str]] = []
    for entry in stored:
        number = entry.get("shipment_order_number", "")
        if normalise_shipment(number) in already:
            continue
        row = {column: entry.get(column, "") for column in OUTPUT_COLUMNS}
        row["shipment_order_number"] = number
        # The stored verdict is carried, not reset. This pass now reads fixed
        # rows too, and calling one unfixed because it was re-read would undo
        # a fix that already happened.
        row["ko_fixed"] = entry.get("ko_fixed", "") or "no"
        row["source"] = entry.get("source", "") or "table"
        row["batch"] = f"recheck-{run}"
        rows.append(row)
        if args.limit > 0 and len(rows) >= args.limit:
            break

    if not rows:
        return [], {}
    log(f"Re-checking {len(rows)} shipment(s) the table still holds open"
        + (f", leaving out {len(already)} already covered by this run's uploads"
           if already else "") + ".")

    truncated = args.limit > 0 and len(rows) >= args.limit
    if truncated:
        log(f"  --limit {args.limit}: only the first {args.limit} are checked.")

    prefill_status(rows, args, log, client)
    refresh_fulfillment(rows, args, log)
    check_documents(rows, args, log)
    attach_gxo_attempts(rows, args, log, client)
    attach_monitoring(rows, args, log, client)
    # Same as enrich(): a re-checked shipment may have gained a warehouse
    # attempt since the last run, and whether it came after the fix can only
    # be judged once both dates are in. The MERGE works this out in SQL too,
    # but the row is written to this run's CSV before that, and the two should
    # not disagree.
    for row in rows:
        if tried_after_fix(row["ko_fix_date"], row["last_tried_by_gxo_date"]):
            row["tried_by_gxo_after_the_fix"] = "yes"
    # Same reason as in enrich(): the channel may have arrived only now.
    fill_oerodp(rows, log)

    summary = {
        "batch": f"recheck-{run}",
        "csv_file": "", "xlsx_file": "",
        "shipments": len(rows),
        "from_csv": 0, "from_excel": 0, "matched_both": 0,
        "fixed": sum(1 for r in rows if r["ko_fix_date"]),
        "with_warehouse_attempt": sum(1 for r in rows if r["last_tried_by_gxo_date"]),
        "with_error": sum(1 for r in rows if r["ko_reason"]),
        "dry_run": bool(args.dry_run),
        "join": "table",
        "truncated": truncated,
        "recheck_only": True,
    }
    return rows, summary


# Every shipment number the operational table holds, read once per run.
# None means it could not be read, which is not the same as an empty table.
_KNOWN_SHIPMENTS: Dict[str, Optional[set]] = {"value": None, "asked": False}


def known_shipments(client: Any, log: Log) -> Optional[set]:
    """The shipment numbers the table already holds, read once per run."""
    if not _KNOWN_SHIPMENTS["asked"]:
        _KNOWN_SHIPMENTS["asked"] = True
        _KNOWN_SHIPMENTS["value"] = table_module.fetch_known_shipments(client, log)
    return _KNOWN_SHIPMENTS["value"]


def drop_fulfilled_newcomers(rows: List[Dict[str, str]], log: Log,
                             client: Any) -> List[Dict[str, str]]:
    """
    A shipment Kibo calls FULFILLED that the table has never held is not logged.

    A KO is a shipment that needs work; one that has already left the
    warehouse needs none. Logging it only to archive it in the same run would
    put a row in the history table that was never a KO to anybody.

    A shipment the table already holds is kept and settled the normal way: it
    was logged when it still was a KO, the record of that is worth having, and
    the resolve pass archives it as fixed and resolved.

    If the table could not be read, nothing is dropped. Guessing wrong here
    loses a KO silently, and a row too many is cheaper than a row missing.
    """
    if not FULFILLED_BY_KIBO:
        return rows
    known = known_shipments(client, log)
    if known is None:
        log("  The table's shipment numbers could not be read, so no FULFILLED "
            "shipment is dropped; they are logged and settled as usual.")
        return rows
    kept: List[Dict[str, str]] = []
    dropped: List[str] = []
    for row in rows:
        number = normalise_shipment(row["shipment_order_number"])
        if number in FULFILLED_BY_KIBO and number not in known:
            dropped.append(number)
            continue
        kept.append(row)
    for number in dropped:
        # Not in the table, so there is nothing for the resolve pass to
        # archive; leaving it in the set would only make the log overcount.
        FULFILLED_BY_KIBO.discard(number)
    if dropped:
        log(f"  {len(dropped)} shipment(s) Kibo calls FULFILLED are new to the "
            "table and are not logged: they have left the warehouse, so there "
            "is no KO to record.")
    return kept


def enrich(rows: List[Dict[str, str]], args: argparse.Namespace, log: Log,
           client: Any) -> None:
    """
    Everything a newly found shipment needs before it is written.

    The same four steps the uploaded shipments go through, in the same order:
    the documents API decides ko_fixed, the warehouse's own attempts come out
    of BigQuery, the monitoring table fills ko_reason, ko_source,
    blocking_reason, channel and the GXO action, and only then can a retry be
    judged to have happened after the fix.
    """
    # Kibo first: it decides which of these are worth asking the documents
    # API about at all, and it can settle one outright.
    prefill_status(rows, args, log, client)
    refresh_fulfillment(rows, args, log)
    check_documents(rows, args, log)
    attach_gxo_attempts(rows, args, log, client)
    attach_monitoring(rows, args, log, client)
    for row in rows:
        if tried_after_fix(row["ko_fix_date"], row["last_tried_by_gxo_date"]):
            row["tried_by_gxo_after_the_fix"] = "yes"
    # Last, as on the upload path. These rows are created with an empty
    # channel, so the OERODP they were given has no letter; refresh_fulfillment
    # and attach_monitoring have only just supplied the channel it needs.
    fill_oerodp(rows, log)


def gxo_ko_candidates(args: argparse.Namespace, log: Log, client: Any,
                      gxo_ko: Dict[str, str], run: str,
                      seen: Sequence[str]) -> Tuple[List[Dict[str, str]], Dict[str, Any]]:
    """
    Take shipments from the latest gxo_ko import that nothing else has.

    Only for a run with no upload. On a run with one, gxo_ko is already the
    GXO side of the join and these shipments are in it. A shipment the
    operational table holds is skipped — the merge would only rewrite it —
    and so is one already archived, which a warehouse file that still lists it
    should not reopen.
    """
    if not gxo_ko:
        return [], {}
    known = {normalise_shipment(number) for number in seen}
    fresh = {number: logistic for number, logistic in gxo_ko.items()
             if normalise_shipment(number) not in known}
    if fresh:
        fresh = {number: logistic for number, logistic in fresh.items()
                 if normalise_shipment(number) not in
                 table_module.already_known(client, list(fresh), log)}
    if not fresh:
        log("  Every shipment in the latest gxo_ko import is already known.")
        return [], {}

    rows: List[Dict[str, str]] = []
    for number, logistic in sorted(fresh.items()):
        shipment = normalise_shipment(number)
        row = {column: "" for column in OUTPUT_COLUMNS}
        row["shipment_order_number"] = shipment
        row["logistic_order_number"] = str(logistic or "").strip()
        row["brand"] = shipment[:2]
        row["location_provider"] = GXO
        row["source"] = f"{GXO} only"
        row["ko_fixed"] = "no"
        row["in_latest_gxo_input"] = "yes"
        row["batch"] = f"gxo_ko-{run}"
        if row["logistic_order_number"]:
            row["oerodp"] = build_oerodp(shipment, "", row["logistic_order_number"])
        rows.append(row)

    log(f"  {len(rows)} shipment(s) taken from the latest gxo_ko import.")
    enrich(rows, args, log, client)
    summary = {
        "batch": f"gxo_ko-{run}",
        "csv_file": "", "xlsx_file": "",
        "shipments": len(rows),
        "from_csv": 0, "from_excel": 0, "matched_both": 0,
        "fixed": sum(1 for r in rows if r["ko_fix_date"]),
        "with_warehouse_attempt": sum(1 for r in rows if r["last_tried_by_gxo_date"]),
        "with_error": sum(1 for r in rows if r["ko_reason"]),
        "dry_run": bool(args.dry_run),
        "join": "gxo_ko",
        "truncated": False,
        "gxo_ko_only": True,
    }
    return rows, summary


def monitoring_candidates(args: argparse.Namespace, log: Log, client: Any,
                          run: str, seen: Sequence[str]) -> Tuple[List[Dict[str, str]], Dict[str, Any]]:
    """
    Take the shipments the monitoring table calls blocked into the table.

    The monitoring table is read as it stands — this run does not rebuild it,
    whatever keeps it up to date does. Anything it reports with a blocking
    reason — other than the one that says the shipment has already shipped —
    is a KO, whether or not it was in an upload. A
    shipment the operational table already holds is left alone, so this only
    ever adds.

    These shipments then go through the same checks as the rest: the documents
    API, the warehouse's attempts, and the monitoring lookup that fills in
    ko_reason and location_provider.
    """
    candidates = table_module.fetch_ko_candidates(client, log, args.limit)
    if not candidates:
        return [], {}
    known = {normalise_shipment(number) for number in seen}
    rows: List[Dict[str, str]] = []
    for entry in candidates:
        shipment = normalise_shipment(entry.get("shipment_order_number", ""))
        if not shipment or shipment in known:
            continue
        known.add(shipment)
        row = {column: "" for column in OUTPUT_COLUMNS}
        row["shipment_order_number"] = shipment
        row["logistic_order_number"] = entry.get("logistic_order_number", "")
        row["oms_shipment_number"] = entry.get("oms_shipment_number", "")
        row["order_submit_date"] = entry.get("order_submit_date", "")
        row["shipment_creation_date"] = entry.get("shipment_creation_date", "")
        row["initial_fulfillment_status"] = entry.get("fulfillment_status", "")
        row["updated_fulfillment_status"] = ""
        row["location_provider"] = (entry.get("location_provider", "") or "").upper()
        row["brand"] = entry.get("brand", "") or shipment[:2]
        row["channel"] = entry.get("channel", "")
        # ko_reason is left to attach_monitoring, which reads the same table
        # and applies the priority the dashboard expects; the blocking reason
        # is only the fallback for a row it cannot match.
        row["blocking_reason"] = entry.get("blocking_reason", "")
        row["ko_source"] = entry.get("ko_source", "")
        row["_ko_status"] = entry.get("blocking_reason", "")
        row["ko_fixed"] = "no"
        row["source"] = MONITORING_SOURCE
        row["batch"] = f"monitoring-{run}"
        # OERODP can only be built when the logistics order number is known;
        # without one there is nothing to put after the hyphen.
        if row["logistic_order_number"]:
            row["oerodp"] = build_oerodp(shipment, "", row["logistic_order_number"])
        rows.append(row)

    if not rows:
        log("  Every blocked shipment of the monitoring table is already in this run.")
        return [], {}
    log(f"  {len(rows)} shipment(s) taken from the monitoring table.")

    enrich(rows, args, log, client)

    summary = {
        "batch": f"monitoring-{run}",
        "csv_file": "", "xlsx_file": "",
        "shipments": len(rows),
        "from_csv": 0, "from_excel": 0, "matched_both": 0,
        "fixed": sum(1 for r in rows if r["ko_fix_date"]),
        "with_warehouse_attempt": sum(1 for r in rows if r["last_tried_by_gxo_date"]),
        "with_error": sum(1 for r in rows if r["ko_reason"]),
        "dry_run": bool(args.dry_run),
        "join": "monitoring",
        "truncated": False,
        "monitoring_only": True,
    }
    return rows, summary


def process_batch(
    key: str, csv_path: Optional[Path], xlsx_path: Optional[Path],
    args: argparse.Namespace, log: Log,
    client: Any = None, gxo_ko: Optional[Dict[str, str]] = None,
) -> Tuple[List[Dict[str, str]], Dict[str, Any]]:
    log(f"Batch {key}"
        + ("" if csv_path and xlsx_path
           else f" ({'CSV' if csv_path else 'workbook'} only)"))
    csv_rows = collapse_csv(read_csv(csv_path, log), log) if csv_path else {}
    xlsx_rows = collapse_workbook(read_workbook(xlsx_path, log)) if xlsx_path else {}

    # The join describes how two sides meet, so with only one side it has
    # nothing to say: 'csv' and 'inner' would both throw the batch away. The
    # one file that was uploaded is the batch, and the run says so rather than
    # reporting nought shipments and leaving someone to work out why.
    join = args.join
    if not (csv_path and xlsx_path) and join != "outer":
        log(f"  --join {join} needs both files; this batch has one, "
            "so every shipment in it is taken.")
        join = "outer"
    rows = build_rows(csv_rows, xlsx_rows, key, join, gxo_ko)
    log(f"  {len(rows)} shipment(s) to check "
        f"({sum(1 for r in rows if r['source'] == 'TLG + GXO')} on both sides).")

    truncated = args.limit > 0 and len(rows) > args.limit
    if truncated:
        log(f"  --limit {args.limit}: only the first {args.limit} are checked.")
        rows = rows[:args.limit]

    skip_already_fixed(rows, args, log, client)
    prefill_status(rows, args, log, client)
    refresh_fulfillment(rows, args, log)
    check_documents(rows, args, log)
    attach_gxo_attempts(rows, args, log, client)
    attach_monitoring(rows, args, log, client)

    summary = {
        "batch": key,
        "csv_file": csv_path.name if csv_path else "",
        "xlsx_file": xlsx_path.name if xlsx_path else "",
        "shipments": len(rows),
        "from_csv": sum(1 for r in rows if r["source"] != "GXO only"),
        "from_excel": sum(1 for r in rows if r["source"] != "TLG only"),
        "matched_both": sum(1 for r in rows if r["source"] == "TLG + GXO"),
        "fixed": sum(1 for r in rows if r["ko_fix_date"]),
        "with_warehouse_attempt": sum(1 for r in rows if r["last_tried_by_gxo_date"]),
        "with_error": sum(1 for r in rows if r["ko_reason"]),
        "dry_run": bool(args.dry_run),
        "join": join,
        "truncated": truncated,
    }
    return rows, summary


# ---------------------------------------------------------------------------
# Writing the dataset out
# ---------------------------------------------------------------------------

def write_csv(path: Path, rows: Sequence[Dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=OUTPUT_COLUMNS, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column, "") for column in OUTPUT_COLUMNS})


def discard(paths: Sequence[Optional[Path]], log: Log, why: str) -> None:
    """
    Delete the uploaded files once the run has read them.

    They used to be moved into archive/ or error/, which meant every upload
    was committed to the repository twice and stayed there. Nothing read them
    back: the run's own dataset under output/ and its log under logs/ are the
    record of what a run saw. So they are deleted, and what they contained
    lives on in the table and the log rather than in a folder nobody opens.

    A file that will not delete is reported and left alone. It is in the
    repository's working copy, not in the table, so the worst case is that the
    next run reads the same batch again.
    """
    for path in paths:
        if path is None:
            continue
        try:
            path.unlink(missing_ok=True)
            log(f"  {path.name} deleted ({why}).")
        except OSError as exc:
            log(f"  {path.name} could not be deleted: {exc}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input-dir", default="ko_monitoring/input")
    parser.add_argument("--output-dir", default="ko_monitoring/output")
    parser.add_argument("--logs-dir", default="ko_monitoring/logs")
    parser.add_argument("--auth-url", default=os.environ.get("GET_DOCUMENTS_AUTH_URL") or AUTH_URL)
    parser.add_argument("--url", default=os.environ.get("GET_DOCUMENTS_URL") or POST_URL)
    parser.add_argument("--probe", default="",
                        help="comma-separated shipment numbers: call the documents "
                             "API for each and print the request and the response, "
                             "writing nothing")
    parser.add_argument("--probe-variants", action="store_true",
                        help="with --probe: send each candidate body for the "
                             "same shipment and print what each one returns, "
                             "to settle which field the endpoint reads")
    parser.add_argument("--probe-logistic", default="",
                        help="with --probe, the logisticOrderNumber to send instead "
                             "of the one in the table")
    parser.add_argument("--kibo-auth-url",
                        default=os.environ.get("KIBO_AUTH_URL")
                        or "https://home.mozu.com/api/platform/applications/authtickets/oauth")
    parser.add_argument("--kibo-host",
                        default=os.environ.get("KIBO_HOST") or "tp3.mozu.com")
    parser.add_argument("--tenant-map", default=os.environ.get("KIBO_TENANT_MAP") or "")
    parser.add_argument("--default-tenant-id",
                        default=os.environ.get("KIBO_TENANT_ID") or "")
    parser.add_argument("--default-site-id",
                        default=os.environ.get("KIBO_SITE_ID") or "")
    parser.add_argument("--timeout", type=float,
                        default=float(os.environ.get("KIBO_TIMEOUT") or 60))
    parser.add_argument("--moda-url",
                        default=os.environ.get("MODA_GET_LABEL_URL") or MODA_URL,
                        help="where MODA shipments are checked")
    parser.add_argument("--dry-run", action="store_true",
                        help="join the two files and report, call nothing")
    parser.add_argument("--backfill", action="store_true",
                        help="fill in the descriptive columns of every shipment "
                             "in the operational table and the archive: oerodp, "
                             "oms_shipment_number, ko_source, blocking_reason, "
                             "order_submit_date, shipment_creation_date, "
                             "fulfillment_status, location_provider and channel. "
                             "The monitoring table first, Kibo's getShipments as "
                             "the fallback for all of them. Calls no documents "
                             "API and changes no verdict")
    parser.add_argument("--bq-lookback-days", type=int, default=0,
                        help="only read attempts from the last N days (0 = all)")
    parser.add_argument("--label-check", dest="zpl_check",
                        choices=["printable", "zpl", "any"], default="printable",
                        help="which label formats count: printable = ZPL or PDF "
                             "(the default, both of which GXO can print), zpl = ZPL "
                             "only, any = a populated label of any format")
    parser.add_argument("--join", choices=["outer", "csv", "inner"], default="outer",
                        help="which shipments the dashboard holds: outer = every CSV "
                             "shipment plus the ones the filtered workbook adds, "
                             "csv = the CSV only, inner = only shipments on both sides")
    parser.add_argument("--limit", type=int, default=0, help="check at most N shipments (0 = all)")
    parser.add_argument("--sleep", type=float, default=0.1, help="seconds between API calls")
    parser.add_argument("--max-retries", type=int, default=3)
    parser.add_argument("--keep-input", action="store_true",
                        help="do not delete the uploaded files after reading them")
    return parser.parse_args(argv)


def run_probe(args: argparse.Namespace, log: Log) -> int:
    """
    Call the documents API for named shipments and print the whole exchange.

    A diagnostic, not a stage: it writes nothing, decides nothing and touches
    no table. It exists because a shipment can come back one way in Postman
    and another way here, and the only way to settle that is to see the
    request this code actually sends and the response it actually gets.

    Label and document data are cut to a length — the point is which fields
    came back and what they are called, not 100 kB of base64.
    """
    numbers = [part.strip() for part in str(args.probe or "").split(",") if part.strip()]
    if not numbers:
        log("--probe needs at least one shipment number.")
        return 2

    client = bigquery_client(log)
    logistics: Dict[str, str] = {}
    if client is not None and not args.probe_logistic:
        # The logistics order number goes in the body, so it is taken from the
        # table rather than guessed: a probe that sends a different body from
        # the run it is diagnosing would prove nothing.
        for entry in table_module.fetch_all(client, log, 0):
            number = normalise_shipment(entry.get("shipment_order_number", ""))
            if number:
                logistics[number] = str(entry.get("logistic_order_number", "") or "")

    session = make_session(args.max_retries)
    token = get_access_token(session, args.auth_url)
    log(f"Authenticated against {args.auth_url}")
    log("")

    for number in numbers:
        logistic = args.probe_logistic or logistics.get(normalise_shipment(number), "")

        if args.probe_variants:
            # Each candidate body against the same shipment, one after
            # another. The endpoint is the judge: whichever body comes back
            # eligible is the body this workflow should be sending.
            log(f"=== {number} — {len(body_variants(number, logistic))} bodies ===")
            log(f"  POST {args.url}")
            log(f"  logistic_order_number in the table: {logistic or '(none)'}")
            for label, candidate in body_variants(number, logistic, "", ""):
                log(f"  -- {label}")
                log(f"     sent: {json.dumps(candidate)}")
                try:
                    response = session.post(
                        args.url,
                        headers={"Authorization": f"Bearer {token}",
                                 "Content-Type": "application/json"},
                        json=candidate, timeout=120,
                    )
                except requests.RequestException as exc:
                    log(f"     the call failed: {exc}")
                    continue
                try:
                    payload = response.json()
                except ValueError:
                    payload = None
                if not isinstance(payload, dict):
                    log(f"     HTTP {response.status_code}, no JSON object: "
                        f"{(response.text or '')[:200]}")
                    continue
                flags = read_response(payload, args.zpl_check)
                fixed, why = judge(flags)
                log(f"     HTTP {response.status_code} · "
                    f"eligible={flags['eligible']!r} · "
                    f"orderNumber={payload.get('orderNumber')!r} · "
                    f"logisticOrderNumber={payload.get('logisticOrderNumber')!r} · "
                    f"errorMessage={str(payload.get('errorMessage') or '')[:80]!r}")
                log(f"     verdict: {fixed}, {why}")
                if args.sleep:
                    time.sleep(args.sleep)
            log("")
            continue

        body = build_body(number, logistic, "", "")
        log(f"=== {number} ===")
        log(f"  POST {args.url}")
        log(f"  logistic_order_number in the table: {logistic or '(none)'}")
        log(f"  request: {json.dumps(body)}")
        try:
            response = session.post(
                args.url,
                headers={"Authorization": f"Bearer {token}",
                         "Content-Type": "application/json"},
                json=body, timeout=120,
            )
        except requests.RequestException as exc:
            log(f"  the call failed: {exc}")
            log("")
            continue
        log(f"  HTTP {response.status_code} {response.reason}")
        try:
            payload = response.json()
        except ValueError:
            log(f"  body (not JSON): {(response.text or '')[:600]}")
            log("")
            continue
        if not isinstance(payload, dict):
            log(f"  body is {type(payload).__name__}, not an object: "
                f"{json.dumps(payload)[:600]}")
            log("")
            continue

        log(f"  top-level keys: {', '.join(sorted(payload)) or '(none)'}")
        for key in sorted(payload):
            value = payload[key]
            if key in ("labelDocuments", "invoiceDocuments") and isinstance(value, list):
                log(f"  {key}: {len(value)} entry/entries")
                for item in value:
                    if not isinstance(item, dict):
                        continue
                    shown = {
                        name: (f"<{len(str(inner))} chars>"
                               if name in ("labelData", "docData") and inner else inner)
                        for name, inner in item.items()
                    }
                    log(f"    {json.dumps(shown)[:400]}")
            elif isinstance(value, (dict, list)):
                log(f"  {key}: {json.dumps(value)[:300]}")
            else:
                log(f"  {key}: {value!r}")

        flags = read_response(payload, args.zpl_check)
        log(f"  read as: eligible={flags['eligible']!r}, "
            f"SHPLBL populated={flags['shplbl_populated']} {flags['shplbl_format']}, "
            f"RETLBL present={flags['retlbl_present']} populated={flags['retlbl_populated']}")
        log(f"  verdict: {judge(flags)}")
        log("")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    log = Log()

    # --probe is a diagnostic and nothing else: it calls the API for the
    # shipments named, prints what went out and what came back, and stops.
    if args.probe:
        outcome = run_probe(args, log)
        print(log.text())
        logs_dir = Path(args.logs_dir)
        logs_dir.mkdir(parents=True, exist_ok=True)
        (logs_dir / "probe.log").write_text(log.text(), encoding="utf-8")
        return outcome
    started = now_utc()
    run_stamp = stamp(started)

    input_dir = Path(args.input_dir)
    input_dir.mkdir(parents=True, exist_ok=True)
    complete, incomplete = find_batches(input_dir)

    log(f"KO monitoring run {run_stamp}")
    log(f"Batches  : {len(complete)} complete, {len(incomplete)} incomplete")
    log("")

    for key, files in incomplete:
        log(f"Batch {key}: waiting for its pair — "
            + ", ".join(path.name for path in files)
            + ". A batch needs one CSV and one .xlsx.")

    all_rows: List[Dict[str, str]] = []
    summaries: List[Dict[str, Any]] = []
    failed_batches = 0

    # One client for the whole run: the already-fixed lookup, the warehouse
    # attempts, the open-shipment re-check and the table write all go through it.
    client = None
    if True:
        client = bigquery_client(log)
    # The warehouse's own project: its KO list, and the SHIPPED events that
    # say a shipment has left. Read once, used by every batch and by resolve.
    wlfs_client = wlfs_bigquery_client(log)
    # gxo_ko is read on every run, upload or not: a shipment the warehouse has
    # just added to its KO file is a KO whether or not anyone uploaded
    # anything today. On a run with an upload it also forms the GXO side of
    # the join; on a run without one it only ever adds shipments.
    gxo_ko: Dict[str, str] = {}
    if wlfs_client is not None and not args.backfill:
        gxo_ko = table_module.fetch_gxo_ko(wlfs_client, log)

    # --backfill is its own pass and ends the run: both tables, nine
    # descriptive columns, nothing decided and nothing resolved. It writes
    # through update_columns rather than the merge, so it does not go through
    # all_rows at all. Files waiting in the queue are left for the next run.
    if args.backfill:
        if client is None or table_module is None:
            log("")
            log("Records: --backfill needs the table, and there is no "
                + ("client for it." if table_module else
                   "ko_operational_table.py beside this script."))
            logs_dir = Path(args.logs_dir)
            logs_dir.mkdir(parents=True, exist_ok=True)
            (logs_dir / f"run-{run_stamp}.log").write_text(log.text(), encoding="utf-8")
            return 0
        outcome = backfill_all(args, log, client, run_stamp)
        log("")
        log("Records  : backfill only — no documents API, no verdicts, nothing archived.")
        for name in ("operational", "archive"):
            part = outcome.get(name) or {}
            if part:
                log(f"{name.title():9}: {part['rows']} row(s) read, "
                    f"{part['updated']} updated, {part['asked_kibo']} asked of Kibo"
                    + (f" — {part['error']}" if part.get("error") else ""))
        logs_dir = Path(args.logs_dir)
        logs_dir.mkdir(parents=True, exist_ok=True)
        (logs_dir / f"run-{run_stamp}.log").write_text(log.text(), encoding="utf-8")
        return 0

    # An empty queue is not an idle run. A KO is fixed by the warehouse at its
    # own pace, so every run asks again about the shipments the table still
    # calls unfixed, upload or no upload.
    if not complete and client is None:
        log("")
        log("Records: nothing to do — no files to process and no table to re-check.")
        logs_dir = Path(args.logs_dir)
        logs_dir.mkdir(parents=True, exist_ok=True)
        (logs_dir / f"run-{run_stamp}.log").write_text(log.text(), encoding="utf-8")
        return 0

    for key, csv_path, xlsx_path in complete:
        try:
            rows, summary = process_batch(key, csv_path, xlsx_path, args, log,
                                          client, gxo_ko)
        except Exception as exc:  # noqa: BLE001 - one bad batch must not stop the rest
            failed_batches += 1
            log(f"  Batch {key} failed: {exc}")
            if not args.keep_input:
                discard((csv_path, xlsx_path), log, "the batch failed")
            continue
        all_rows.extend(rows)
        summaries.append(summary)
        if not args.keep_input and not args.dry_run:
            discard((csv_path, xlsx_path), log, "processed")
        log("")

    # ---- shipments the latest gxo_ko import carries ------------------------
    # Only when no upload came in: with one, gxo_ko is already the GXO side of
    # the join and these shipments are in it.
    if (client is not None and not complete
            and table_module is not None and gxo_ko):
        try:
            rows, summary = gxo_ko_candidates(
                args, log, client, gxo_ko, run_stamp,
                [row["shipment_order_number"] for row in all_rows],
            )
        except Exception as exc:  # noqa: BLE001 - the rest of the run still stands
            log(f"  The gxo_ko shipments could not be taken: {exc}")
            rows, summary = [], {}
        if rows:
            all_rows.extend(rows)
            summaries.append(summary)
            log("")

    # ---- shipments the monitoring table reports as blocked -----------------
    # Taken on every run, upload or no upload, and after the batches so that a
    # shipment this run already carries is not added a second time. A backfill
    # adds nothing: it fills in what is there rather than finding more.
    # Every run asks Kibo about every shipment the table still holds open,
    # upload or no upload: the live fulfilment status is the point, and a
    # shipment nobody uploaded today can still have shipped, been cancelled or
    # moved on since the last run. The batches above have just covered their
    # own shipments, so those are left out rather than asked twice.
    if client is not None and table_module is not None:
        try:
            rows, summary = recheck_open(
                args, log, client, run_stamp,
                skip=[row["shipment_order_number"] for row in all_rows],
            )
        except Exception as exc:  # noqa: BLE001 - the rest of the run still stands
            log(f"  The table's open shipments could not be re-checked: {exc}")
            rows, summary = [], {}
        if rows:
            all_rows.extend(rows)
            summaries.append(summary)
            log("")

    if client is not None and table_module is not None:
        try:
            rows, summary = monitoring_candidates(
                args, log, client, run_stamp,
                [row["shipment_order_number"] for row in all_rows],
            )
        except Exception as exc:  # noqa: BLE001 - the rest of the run still stands
            log(f"  The monitoring table's shipments could not be taken: {exc}")
            rows, summary = [], {}
        if rows:
            all_rows.extend(rows)
            summaries.append(summary)
            log("")

    # ---- the KO operational table -----------------------------------------
    # The dataset under output/ is this run; the table is every run. A shipment
    # already there is updated rather than added again, and one that has
    # dropped out of the uploads is marked ko_resolved instead of deleted.
    table_outcome: Dict[str, Any] = {}
    if all_rows and client is not None and table_module is not None:
        all_rows = drop_fulfilled_newcomers(all_rows, log, client)
    if all_rows:
        if table_module is None:
            log("ko_operational_table.py is not beside this script; the table was not written.")
        else:
            if client is None:
                log(f"  {table_module.FQN} was not written: no BigQuery client.")
            else:
                table_outcome = table_module.write(
                    client, all_rows, run_stamp, log, dry_run=args.dry_run,
                )
                # Resolving is its own pass: the rule depends on who holds the
                # shipment, not on what this upload contained. A run that saw
                # no workbook and no gxo_ko passes None, and then nothing is
                # resolved for absence.
                if not args.dry_run:
                    gxo_present = None
                    # A workbook, specifically. Now that either file can be
                    # uploaded on its own, a run can have a batch and still
                    # have seen nothing from GXO, and a CSV-only run is no
                    # evidence at all that a GXO shipment has gone away.
                    workbook_seen = any(item.get("xlsx_file") for item in summaries)
                    if workbook_seen and not any(
                        item.get("truncated") for item in summaries
                    ):
                        gxo_present = {
                            normalise_shipment(row["shipment_order_number"])
                            for row in all_rows
                            if GXO in normalise_source(row.get("source", "")).upper()
                        }
                    # Which shipments the latest workbook and gxo_ko carry.
                    # Only after a run that had both: otherwise "the latest
                    # upload" would quietly become "the latest run".
                    if gxo_present is not None:
                        table_module.mark_latest_inputs(
                            client, sorted(gxo_present), run_stamp, log)
                    table_outcome["resolved"] = run_resolve(
                        client, wlfs_client, gxo_present, log, run_stamp)
                    # A resolved shipment leaves the operational table: it is
                    # copied into the history table and then removed, so the
                    # table the dashboard reads holds open shipments only.
                    archived = table_module.archive_resolved(client, run_stamp, log)
                    table_outcome["archived"] = archived.get("archived", 0)
                    if archived.get("error"):
                        table_outcome["archive_error"] = archived["error"]
                still_open = table_module.open_count(client, log)
                if still_open is not None:
                    log(f"  {table_module.FQN}: {still_open} shipment(s) still open.")
                    table_outcome["still_open"] = still_open
        log("")

    if all_rows:
        output_dir = Path(args.output_dir)
        dataset = output_dir / f"ko_monitoring-{run_stamp}.csv"
        write_csv(dataset, all_rows)
        write_csv(output_dir / "latest.csv", all_rows)
        meta = {
            "run": run_stamp,
            "built_at": iso(started),
            "finished_at": iso(now_utc()),
            "file": dataset.name,
            "columns": OUTPUT_COLUMNS,
            "rows": len(all_rows),
            "batches": summaries,
            "dry_run": bool(args.dry_run),
            "zpl_check": args.zpl_check,
            "table": table_outcome,
        }
        (output_dir / "latest.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
        log(f"Dataset  : {dataset} ({len(all_rows)} row(s)), and output/latest.csv")

    log("")
    log(f"Records  : {len(all_rows)} shipment(s) over {len(summaries)} batch(es), "
        f"{failed_batches} batch(es) failed")
    log(f"Fixed    : {sum(1 for r in all_rows if r['ko_fix_date'])}")
    if table_outcome:
        log(f"Table    : {table_outcome.get('written', 0)} merged, "
            f"{table_outcome.get('resolved', 0)} resolved, "
            f"{table_outcome.get('archived', 0)} archived"
            + (f", {table_outcome['error']}" if table_outcome.get("error") else ""))
    log(f"Finished : {iso(now_utc())}")

    logs_dir = Path(args.logs_dir)
    logs_dir.mkdir(parents=True, exist_ok=True)
    (logs_dir / f"run-{run_stamp}.log").write_text(log.text(), encoding="utf-8")
    if all_rows:
        write_csv(logs_dir / f"run-{run_stamp}.csv", all_rows)

    return 0


if __name__ == "__main__":
    sys.exit(main())
