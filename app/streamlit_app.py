#!/usr/bin/env python3
"""
Kibo cancellation console.

A password-protected page that drops cancellation CSV files into the input/
folder of the GitHub repository, where the scheduled workflow picks them up,
and reads the run logs back out.

Everything it needs lives in Streamlit secrets; see .streamlit/secrets.toml.example.
"""
from __future__ import annotations

import base64
import csv
import hashlib
import hmac
import io
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests
import streamlit as st

GITHUB_API = "https://api.github.com"
CSV_COLUMNS = ["shipmentOrderNumber", "productId", "orderNumber", "quantity", "reasonCode", "moreInfo"]

# The two-letter brand prefix that opens every shipmentOrderNumber and
# orderNumber decides the Kibo tenant. Kept in step with BRAND_TENANTS in
# scripts/process_cancellations.py; the app only uses it to catch a typo
# before the record is queued, the worker is what actually resolves it.
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


def brand_prefix(*values: str) -> str:
    for value in values:
        text = str(value or "").strip().upper()
        if len(text) >= 2 and text[:2].isalpha():
            return text[:2]
    return ""


def unknown_prefixes(rows: List[Dict[str, str]]) -> List[str]:
    """Brand prefixes the worker will not be able to turn into a tenant."""
    found = set()
    for row in rows:
        if row.get("tenantId"):
            continue
        prefix = brand_prefix(row.get("shipmentOrderNumber", ""), row.get("orderNumber", ""))
        if prefix and prefix not in BRAND_TENANTS:
            found.add(prefix)
        elif not prefix and (row.get("shipmentOrderNumber") or row.get("orderNumber")):
            found.add("(no letters)")
    return sorted(found)


REASON_CODES = [
    "CustomerChangedTheirMind",
    "No Inventory",
    "Fraud",
    "Damaged",
    "Other",
]


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class Settings:
    token: str
    repo: str
    branch: str
    input_dir: str
    logs_dir: str
    archive_dir: str
    error_dir: str


def settings() -> Settings:
    section = st.secrets.get("github", {})
    missing = [key for key in ("token", "repo") if not section.get(key)]
    if missing:
        st.error(
            "The app is not configured yet: the `github` section of the secrets is missing "
            + ", ".join(f"`{key}`" for key in missing)
            + ". See .streamlit/secrets.toml.example."
        )
        st.stop()
    return Settings(
        token=section["token"],
        repo=section["repo"],
        branch=section.get("branch", "main"),
        input_dir=section.get("input_dir", "input").strip("/"),
        logs_dir=section.get("logs_dir", "logs").strip("/"),
        archive_dir=section.get("archive_dir", "archive").strip("/"),
        error_dir=section.get("error_dir", "error").strip("/"),
    )


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

def verify_password(password: str, stored: str) -> bool:
    """
    Check a password against 'pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>'.

    A plain string with no '$' is treated as a plaintext password, which is only
    meant for a first local run; the README explains how to hash properly.
    """
    stored = str(stored)
    if "$" not in stored:
        return hmac.compare_digest(password, stored)
    try:
        algorithm, iterations, salt_hex, expected_hex = stored.split("$", 3)
    except ValueError:
        return False
    if algorithm != "pbkdf2_sha256":
        return False
    try:
        derived = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iterations)
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(derived.hex(), expected_hex)


def login_gate() -> str:
    """Render the login form and return the signed-in username."""
    if st.session_state.get("username"):
        return st.session_state["username"]

    users = st.secrets.get("auth", {})
    if not users:
        st.error("No user is configured. Add an `[auth]` section to the secrets.")
        st.stop()

    st.markdown("### Sign in")
    st.caption("Cancellation console for Kibo shipments.")
    with st.form("login"):
        username = st.text_input("Username", autocomplete="username")
        password = st.text_input("Password", type="password", autocomplete="current-password")
        submitted = st.form_submit_button("Sign in", use_container_width=True)

    if submitted:
        stored = users.get(username.strip())
        if stored and verify_password(password, stored):
            st.session_state["username"] = username.strip()
            st.rerun()
        else:
            st.error("Wrong username or password.")
    st.stop()
    return ""  # unreachable


# ---------------------------------------------------------------------------
# GitHub
# ---------------------------------------------------------------------------

class GitHub:
    def __init__(self, config: Settings) -> None:
        self.config = config
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {config.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            }
        )

    def put_file(self, path: str, content: bytes, message: str) -> Tuple[bool, str]:
        url = f"{GITHUB_API}/repos/{self.config.repo}/contents/{path}"
        body = {
            "message": message,
            "content": base64.b64encode(content).decode("ascii"),
            "branch": self.config.branch,
        }
        try:
            response = self.session.put(url, json=body, timeout=30)
        except requests.RequestException as exc:
            return False, str(exc)
        if 200 <= response.status_code < 300:
            return True, ""
        return False, f"HTTP {response.status_code}: {response.text[:400]}"

    def list_dir(self, path: str) -> Tuple[List[Dict[str, Any]], str]:
        url = f"{GITHUB_API}/repos/{self.config.repo}/contents/{path}"
        try:
            response = self.session.get(url, params={"ref": self.config.branch}, timeout=30)
        except requests.RequestException as exc:
            return [], str(exc)
        if response.status_code == 404:
            return [], ""
        if not (200 <= response.status_code < 300):
            return [], f"HTTP {response.status_code}: {response.text[:400]}"
        try:
            payload = response.json()
        except ValueError:
            return [], "The directory listing was not JSON."
        if not isinstance(payload, list):
            return [], "That path is not a directory."
        return [entry for entry in payload if entry.get("type") == "file"], ""

    def read_file(self, path: str) -> Tuple[str, str]:
        url = f"{GITHUB_API}/repos/{self.config.repo}/contents/{path}"
        try:
            response = self.session.get(
                url,
                params={"ref": self.config.branch},
                headers={"Accept": "application/vnd.github.raw"},
                timeout=30,
            )
        except requests.RequestException as exc:
            return "", str(exc)
        if not (200 <= response.status_code < 300):
            return "", f"HTTP {response.status_code}: {response.text[:400]}"
        return response.text, ""

    def dispatch_workflow(self, workflow_file: str, inputs: Dict[str, str]) -> Tuple[bool, str]:
        url = (
            f"{GITHUB_API}/repos/{self.config.repo}/actions/workflows/"
            f"{workflow_file}/dispatches"
        )
        try:
            response = self.session.post(
                url, json={"ref": self.config.branch, "inputs": inputs}, timeout=30
            )
        except requests.RequestException as exc:
            return False, str(exc)
        if 200 <= response.status_code < 300:
            return True, ""
        return False, f"HTTP {response.status_code}: {response.text[:400]}"


# ---------------------------------------------------------------------------
# CSV handling
# ---------------------------------------------------------------------------

ALIASES = {
    "shipmentordernumber": "shipmentOrderNumber",
    "son": "shipmentOrderNumber",
    "productid": "productId",
    "productcode": "productId",
    "sku": "productId",
    "upc": "productId",
    "ordernumber": "orderNumber",
    "externalorderid": "orderNumber",
    "externalordernumber": "orderNumber",
    "quantity": "quantity",
    "qty": "quantity",
    "reasoncode": "reasonCode",
    "reason": "reasonCode",
    "moreinfo": "moreInfo",
    "tenantid": "tenantId",
    "siteid": "siteId",
}


def normalize_header(value: str) -> str:
    return re.sub(r"[\s_\-]+", "", str(value or "").strip().lower())


def parse_uploaded_csv(raw: bytes) -> Tuple[List[Dict[str, str]], List[str]]:
    """Return (rows, problems). Rows use the canonical column names."""
    problems: List[str] = []
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
        problems.append("The file is not UTF-8; it was read as Latin-1.")

    if not text.strip():
        return [], ["The file is empty."]

    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel  # type: ignore[assignment]

    reader = csv.reader(io.StringIO(text), dialect)
    try:
        header = next(reader)
    except StopIteration:
        return [], ["The file has no header row."]

    mapping: Dict[int, str] = {}
    unknown: List[str] = []
    for index, name in enumerate(header):
        canonical = ALIASES.get(normalize_header(name))
        if canonical:
            mapping.setdefault(index, canonical)
        elif str(name).strip():
            unknown.append(str(name).strip())

    if unknown:
        problems.append("Ignored column(s): " + ", ".join(unknown) + ".")

    known = set(mapping.values())
    if not ({"shipmentOrderNumber", "orderNumber"} & known):
        problems.append(
            "The header needs a shipmentOrderNumber or an orderNumber column. "
            "Found: " + ", ".join(str(name) for name in header) + "."
        )
        return [], problems

    rows: List[Dict[str, str]] = []
    for row_number, row in enumerate(reader, start=2):
        if not row or all(not str(cell).strip() for cell in row):
            continue
        record = {column: "" for column in CSV_COLUMNS}
        for index, canonical in mapping.items():
            if index < len(row) and canonical in record:
                record[canonical] = str(row[index]).strip()
        if not (record["shipmentOrderNumber"] or record["orderNumber"]):
            problems.append(
                f"Row {row_number} has neither a shipmentOrderNumber nor an orderNumber; "
                "the workflow will report it as failed."
            )
        rows.append(record)

    if not rows:
        problems.append("The file has a header but no data rows.")
    return rows, problems


def rows_to_csv(rows: List[Dict[str, str]]) -> bytes:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=CSV_COLUMNS, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({column: row.get(column, "") for column in CSV_COLUMNS})
    return buffer.getvalue().encode("utf-8")


def safe_slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-")
    return slug[:60] or "upload"


def build_filename(username: str, kind: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{stamp}__{safe_slug(username)}__{safe_slug(kind)}.csv"


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

def page_style() -> None:
    st.markdown(
        """
        <style>
          .block-container { max-width: 1040px; padding-top: 2.2rem; }
          div[data-testid="stMetricValue"] { font-size: 1.5rem; }
          pre { font-size: 0.78rem; line-height: 1.45; }
        </style>
        """,
        unsafe_allow_html=True,
    )


def submit_rows(
    github: GitHub, config: Settings, rows: List[Dict[str, str]], username: str, kind: str
) -> None:
    filename = build_filename(username, kind)
    path = f"{config.input_dir}/{filename}"
    ok, error = github.put_file(
        path,
        rows_to_csv(rows),
        f"Cancellation request: {len(rows)} record(s) from {username}",
    )
    if ok:
        st.success(
            f"Queued {len(rows)} record(s) as `{filename}`. "
            "The workflow picks it up on its next run and writes a log."
        )
        st.session_state["last_submission"] = filename
    else:
        st.error(f"The file could not be committed: {error}")


def tab_upload(github: GitHub, config: Settings, username: str) -> None:
    st.markdown("#### Upload a CSV")
    st.caption(
        "Columns: shipmentOrderNumber, productId, orderNumber. "
        "A productId cancels that item only; an empty productId cancels the whole shipment; "
        "a row with only an orderNumber cancels every shipment of that order."
    )

    uploaded = st.file_uploader("CSV file", type=["csv", "txt"], label_visibility="collapsed")
    if uploaded is None:
        with st.expander("Example file"):
            st.code(
                "shipmentOrderNumber,productId,orderNumber\n"
                "OW-USA00037769,518055266024613,\n"
                "OW-USA00037770,,\n"
                ",,OW-US-00037771\n",
                language="text",
            )
        return

    rows, problems = parse_uploaded_csv(uploaded.getvalue())
    for problem in problems:
        st.warning(problem)
    if not rows:
        return

    preview = [
        dict(row, tenant=BRAND_TENANTS.get(
            brand_prefix(row.get("shipmentOrderNumber", ""), row.get("orderNumber", "")), "?"
        ))
        for row in rows
    ]
    st.dataframe(preview, use_container_width=True, hide_index=True)
    st.caption(f"{len(rows)} record(s) ready.")

    unknown = unknown_prefixes(rows)
    if unknown:
        st.warning(
            "Unrecognised brand prefix: " + ", ".join(unknown) + ". "
            "Those records will fail unless the prefix is added to the worker's tenant map, "
            "or the row carries a tenantId."
        )

    if st.button("Queue these records", type="primary"):
        submit_rows(github, config, rows, username, uploaded.name.rsplit(".", 1)[0])


def tab_single(github: GitHub, config: Settings, username: str) -> None:
    st.markdown("#### Cancel one item, shipment or order")
    st.caption("The entry is turned into a one-row CSV and dropped in the input folder.")

    scope = st.radio(
        "What do you want to cancel?",
        ["A single item", "A whole shipment", "A whole order"],
        horizontal=True,
    )

    with st.form("single"):
        shipment_order_number = ""
        product_id = ""
        order_number = ""
        quantity = ""

        if scope == "A single item":
            column_left, column_right = st.columns(2)
            shipment_order_number = column_left.text_input(
                "shipmentOrderNumber", placeholder="OW-USA00037769"
            )
            product_id = column_right.text_input(
                "productId", placeholder="518055266024613", help="productCode, UPC or SKU"
            )
            quantity = st.text_input(
                "Quantity", placeholder="leave empty to cancel the whole line"
            )
        elif scope == "A whole shipment":
            shipment_order_number = st.text_input(
                "shipmentOrderNumber", placeholder="OW-USA00037769"
            )
        else:
            order_number = st.text_input("orderNumber", placeholder="OW-US-00037769")

        column_left, column_right = st.columns(2)
        reason_code = column_left.selectbox("Reason code", REASON_CODES)
        more_info = column_right.text_input("More info", value="Unspecified")

        submitted = st.form_submit_button("Queue this cancellation", type="primary")

    if not submitted:
        return

    shipment_order_number = shipment_order_number.strip()
    product_id = product_id.strip()
    order_number = order_number.strip()
    quantity = quantity.strip()

    if scope == "A single item" and not (shipment_order_number and product_id):
        st.error("A single item needs both a shipmentOrderNumber and a productId.")
        return
    if scope == "A whole shipment" and not shipment_order_number:
        st.error("Enter a shipmentOrderNumber.")
        return
    if scope == "A whole order" and not order_number:
        st.error("Enter an orderNumber.")
        return
    if quantity and not quantity.isdigit():
        st.error("The quantity has to be a whole number.")
        return

    prefix = brand_prefix(shipment_order_number, order_number)
    if prefix not in BRAND_TENANTS:
        st.error(
            f"'{prefix or shipment_order_number or order_number}' is not a brand the tool knows. "
            "Every number starts with a two-letter brand code such as OW or DJ, which is what "
            "picks the Kibo tenant."
        )
        return
    st.caption(f"Brand {prefix} · Kibo tenant {BRAND_TENANTS[prefix]}")

    submit_rows(
        github,
        config,
        [
            {
                "shipmentOrderNumber": shipment_order_number,
                "productId": product_id,
                "orderNumber": order_number,
                "quantity": quantity,
                "reasonCode": reason_code,
                "moreInfo": more_info.strip(),
            }
        ],
        username,
        {"A single item": "item", "A whole shipment": "shipment", "A whole order": "order"}[scope],
    )


def tab_logs(github: GitHub, config: Settings) -> None:
    st.markdown("#### Run logs")

    if st.button("Refresh"):
        st.cache_data.clear()

    entries, error = github.list_dir(config.logs_dir)
    if error:
        st.error(f"The logs could not be listed: {error}")
        return

    logs = sorted(
        (entry for entry in entries if entry.get("name", "").endswith(".log")),
        key=lambda entry: entry.get("name", ""),
        reverse=True,
    )
    if not logs:
        st.info("No run log yet. The workflow writes one each time it finds a file in input/.")
        return

    choice = st.selectbox("Run", [entry["name"] for entry in logs[:50]])
    content, error = github.read_file(f"{config.logs_dir}/{choice}")
    if error:
        st.error(f"The log could not be read: {error}")
        return

    summary = re.search(r"^Records\s*:\s*(.+)$", content, flags=re.MULTILINE)
    if summary:
        st.caption(summary.group(1))
    st.code(content, language="text")
    st.download_button("Download this log", content, file_name=choice, mime="text/plain")


def tab_queue(github: GitHub, config: Settings) -> None:
    st.markdown("#### Folders")
    columns = st.columns(3)
    for column, (label, path) in zip(
        columns,
        [("Waiting", config.input_dir), ("Archive", config.archive_dir), ("Errors", config.error_dir)],
    ):
        entries, error = github.list_dir(path)
        files = [entry for entry in entries if not entry.get("name", "").startswith(".")]
        with column:
            st.metric(label, "—" if error else len(files))
            if error:
                st.caption(error)
            else:
                for entry in sorted(files, key=lambda item: item["name"], reverse=True)[:10]:
                    st.caption(entry["name"])


def main() -> None:
    st.set_page_config(page_title="Kibo cancellations", page_icon="⟡", layout="centered")
    page_style()

    username = login_gate()
    config = settings()
    github = GitHub(config)

    header_left, header_right = st.columns([4, 1])
    header_left.markdown("## Kibo cancellation console")
    header_left.caption(f"{config.repo} · branch {config.branch}")
    if header_right.button("Sign out"):
        st.session_state.clear()
        st.rerun()

    upload, single, logs, queue = st.tabs(["Upload CSV", "Single cancellation", "Logs", "Folders"])
    with upload:
        tab_upload(github, config, username)
    with single:
        tab_single(github, config, username)
    with logs:
        tab_logs(github, config)
    with queue:
        tab_queue(github, config)


if __name__ == "__main__":
    main()
