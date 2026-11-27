"""
The daily GXO backlog workbook, taken from the mailbox instead of by hand.

GXO Carisio send one email a day, subject "GXO Carisio: backlog OB", carrying
an Excel workbook either as the attachment itself or zipped inside one. Its
rows are the warehouse's own KO list, and they belong in

    tlg-wlfs-prd.monitoring.gxo_ko

which build_ko_monitoring already reads at the start of every run. Until now
somebody downloaded that workbook and uploaded it to the console; this module
is that person.

    VCVAIC   -> orderNumber   the shipment number
    ub_imko  -> location
    reason                    left empty; it is filled from elsewhere
    reportDate, importDate    copied from the table's latest import, so the
                              new rows join the import already in progress
                              rather than starting one of their own
    fileName                  the workbook's own name, which is also how a
                              run tells an emailed workbook from a CSV import

The workbook is read from the message and parsed in memory. Nothing is
written to disk and nothing is archived: the mailbox is the archive, and a
copy in the repository would only go stale.

Three ways to get the workbook, tried in that order:

  A drop bucket, when GXO_BACKLOG_BUCKET is set. A Power Automate flow owned
  by the mailbox's owner writes the attachment to Cloud Storage, and this
  reads it with the service account the run already has. No Entra admin and
  no app registration: the flow runs under permissions the person already
  holds. The object is deleted once its rows are in.

  Microsoft Graph, when GRAPH_TENANT_ID, GRAPH_CLIENT_ID and
  GRAPH_CLIENT_SECRET are set. An app registration with the Mail.Read
  application permission, reading GRAPH_MAILBOX (defaulting to
  SMTP_USERNAME). This is the supported route.

  IMAP against outlook.office365.com with SMTP_USERNAME and SMTP_PASSWORD,
  the same pair the reporting system sends with. Simpler, and it works only
  where the tenant still allows IMAP basic auth — which this one does not:
  it answers "Basic authentication is disabled." SMTP submission is
  unaffected and keeps working, which is why the reporting system is fine.

Either way, a mailbox that cannot be read is reported and the run carries on
without the workbook.
"""
from __future__ import annotations

import base64
import email
import imaplib
import io
import os
import re
import zipfile
from datetime import date, datetime, timedelta, timezone
from email.header import decode_header, make_header
from typing import Any, Dict, List, Optional, Tuple

try:
    import requests
except ImportError:  # pragma: no cover - Graph is simply unavailable
    requests = None  # type: ignore

try:
    from openpyxl import load_workbook
except ImportError:  # pragma: no cover - reported where it matters
    load_workbook = None  # type: ignore

SUBJECT = "GXO Carisio: backlog OB"


def _setting(name: str, default: str) -> str:
    """
    An environment variable's value, or the default when it says nothing.

    `os.getenv(name, default)` is not enough here. A GitHub Actions `env:`
    block that maps an unset repository variable sets the variable to the
    empty string rather than leaving it out, so the default never applies and
    the host comes out blank — which is how this first failed, trying to
    reach ":993".
    """
    return (os.getenv(name) or "").strip() or default


IMAP_HOST = _setting("IMAP_HOST", "outlook.office365.com")
IMAP_PORT = int(_setting("IMAP_PORT", "993"))
IMAP_FOLDER = _setting("IMAP_FOLDER", "INBOX")
# How far back to look. One email a day, so a week is generous and keeps the
# search cheap on a large mailbox.
SEARCH_DAYS = int(_setting("IMAP_SEARCH_DAYS", "7"))

EXCEL_SUFFIXES = (".xlsx", ".xlsm", ".xls")

GRAPH_ROOT = "https://graph.microsoft.com/v1.0"
GRAPH_LOGIN = "https://login.microsoftonline.com"

# The bucket a Power Automate flow drops the workbook into, for tenants where
# reading the mailbox directly is not on the table.
GCS_ROOT = "https://storage.googleapis.com/storage/v1/b"
GCS_SCOPE = "https://www.googleapis.com/auth/devstorage.read_write"

# The two columns wanted out of the workbook, however they are spelled.
def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _header(raw: Any) -> str:
    """A header decoded to plain text, whatever encoding it arrived in."""
    if raw is None:
        return ""
    try:
        return str(make_header(decode_header(str(raw))))
    except Exception:  # noqa: BLE001 - a header is never worth failing on
        return str(raw)


def credentials() -> Tuple[str, str, str]:
    """(username, password, error). The same pair the reporting system uses."""
    username = _text(os.getenv("SMTP_USERNAME"))
    password = os.getenv("SMTP_PASSWORD") or ""
    if os.getenv("SMTP_REMOVE_PASSWORD_SPACES", "").strip().lower() in ("1", "true", "yes"):
        password = password.replace(" ", "")
    if not username or not password:
        return "", "", ("SMTP_USERNAME and SMTP_PASSWORD are not both set, so "
                        "the mailbox cannot be read.")
    return username, password, ""


def bucket_settings() -> Dict[str, str]:
    """The drop bucket, or {} when it is not configured."""
    bucket = _setting("GXO_BACKLOG_BUCKET", "")
    if not bucket:
        return {}
    return {
        "bucket": bucket,
        "prefix": _setting("GXO_BACKLOG_PREFIX", ""),
        # Its own service account if there is one, otherwise one the run
        # already carries. BI first: the bucket is a drop box rather than
        # warehouse data, and the BI account is the one that reads most of
        # what this workflow touches. Any of the three works — the bucket's
        # IAM decides, not this list.
        "creds": (os.getenv("GXO_BACKLOG_CREDS")
                  or os.getenv("BI_BIGQUERY_CREDS")
                  or os.getenv("WLFS_BIGQUERY_CREDS") or ""),
        # Delete the object once its rows are in. The workbook is not to be
        # archived, and the bucket is a letterbox rather than a filing
        # cabinet: left alone it would accumulate a copy a day for ever.
        "delete": _setting("GXO_BACKLOG_DELETE", "1").lower()
                  not in ("0", "false", "no"),
    }


# The service account the bucket calls were made with, for the error message.
_BUCKET_IDENTITY: Dict[str, str] = {"email": ""}


def _bucket_token(settings: Dict[str, str]) -> Tuple[str, str]:
    """(token, error) for the bucket's service account."""
    if requests is None:
        return "", "the requests package is not installed"
    try:
        import json as _json
        from google.auth.transport.requests import Request
        from google.oauth2 import service_account as _sa
    except ImportError as exc:
        return "", f"google-auth is not available: {exc}"
    raw = (settings.get("creds") or "").strip()
    if not raw:
        return "", ("neither GXO_BACKLOG_CREDS nor WLFS_BIGQUERY_CREDS is "
                    "set, so the bucket cannot be read")
    try:
        info = _json.loads(raw)
        credentials = _sa.Credentials.from_service_account_info(
            info, scopes=[GCS_SCOPE])
        credentials.refresh(Request())
    except Exception as exc:  # noqa: BLE001
        return "", f"the bucket sign-in failed: {exc}"
    # Remembered so a refusal can name the account that was refused. Working
    # out which of a project's service accounts a secret holds is otherwise a
    # hunt through IAM, and the answer is right here in the key.
    _BUCKET_IDENTITY["email"] = str(info.get("client_email", "") or "")
    return credentials.token, ""


def find_workbook_bucket(log: Any) -> Tuple[str, Optional[bytes], str]:
    """
    The newest workbook in the drop bucket, downloaded and then removed.

    A Power Automate flow owned by the mailbox's owner writes the attachment
    here. That keeps the whole thing inside permissions the person already
    has: the flow runs as them, and this end reads with the service account
    the run already uses for BigQuery.
    """
    settings = bucket_settings()
    token, error = _bucket_token(settings)
    if error:
        return "", None, error
    headers = {"Authorization": f"Bearer {token}"}
    bucket = settings["bucket"]
    if _BUCKET_IDENTITY["email"]:
        log(f"  Reading gs://{bucket} as {_BUCKET_IDENTITY['email']}.")

    from urllib.parse import quote
    url = (f"{GCS_ROOT}/{quote(bucket, safe='')}/o"
           "?fields=items(name,updated,size)&maxResults=200")
    if settings["prefix"]:
        url += f"&prefix={quote(settings['prefix'], safe='')}"
    try:
        listed = requests.get(url, headers=headers, timeout=120)
    except Exception as exc:  # noqa: BLE001
        return "", None, f"the bucket could not be listed: {exc}"
    if listed.status_code == 403:
        who = _BUCKET_IDENTITY["email"] or "the service account"
        return "", None, (
            f"{who} cannot read gs://{bucket} (HTTP 403). Grant it Storage "
            "Object Admin on the bucket — Viewer is not enough, because the "
            "object is removed once it has been read:\n"
            f"    gcloud storage buckets add-iam-policy-binding gs://{bucket} "
            f'--member="serviceAccount:{who}" '
            '--role="roles/storage.objectAdmin"')
    if listed.status_code != 200:
        return "", None, (f"listing gs://{bucket} returned HTTP "
                          f"{listed.status_code}: {listed.text[:200]}")

    items = [item for item in (listed.json() or {}).get("items", [])
             if item.get("name", "").lower().endswith(EXCEL_SUFFIXES + (".zip",))]
    if not items:
        log(f"  Nothing to collect in gs://{bucket}"
            + (f"/{settings['prefix']}" if settings["prefix"] else "") + ".")
        return "", None, ""
    items.sort(key=lambda item: item.get("updated", ""), reverse=True)
    newest = items[0]
    name = newest["name"]

    try:
        got = requests.get(
            f"{GCS_ROOT}/{quote(bucket, safe='')}/o/{quote(name, safe='')}?alt=media",
            headers=headers, timeout=300)
    except Exception as exc:  # noqa: BLE001
        return "", None, f"{name} could not be downloaded: {exc}"
    if got.status_code != 200:
        return "", None, (f"downloading {name} returned HTTP "
                          f"{got.status_code}: {got.text[:200]}")
    payload = got.content
    log(f"  gs://{bucket}/{name} ({len(payload)} bytes, "
        f"written {newest.get('updated')}).")

    if name.lower().endswith(".zip"):
        inner_name, inner = _excel_in_zip(payload, name, log)
        if inner is None:
            return "", None, f"{name} holds no Excel file"
        found_name, blob = inner_name, inner
    else:
        found_name, blob = name.rsplit("/", 1)[-1], payload

    if settings["delete"]:
        # Only now, with the bytes in hand: a delete before the download
        # would lose the day's workbook to a transient failure.
        try:
            gone = requests.delete(
                f"{GCS_ROOT}/{quote(bucket, safe='')}/o/{quote(name, safe='')}",
                headers=headers, timeout=120)
            if gone.status_code in (200, 204):
                log(f"  gs://{bucket}/{name} removed; the bucket is a "
                    "letterbox, not an archive.")
            else:
                log(f"  gs://{bucket}/{name} could not be removed: HTTP "
                    f"{gone.status_code}. It will be read again tomorrow, and "
                    "the shipments it holds are already in, so nothing is "
                    "inserted twice.")
        except Exception as exc:  # noqa: BLE001
            log(f"  gs://{bucket}/{name} could not be removed: {exc}")
    return found_name, blob, ""


def graph_settings() -> Dict[str, str]:
    """The app registration, or {} when it is not configured."""
    tenant = _setting("GRAPH_TENANT_ID", "")
    client = _setting("GRAPH_CLIENT_ID", "")
    secret = os.getenv("GRAPH_CLIENT_SECRET") or ""
    if not (tenant and client and secret):
        return {}
    return {"tenant": tenant, "client": client, "secret": secret,
            # Whose mailbox to read. With application permissions the app can
            # reach any mailbox the tenant lets it, so it has to be named.
            "mailbox": _setting("GRAPH_MAILBOX", _setting("SMTP_USERNAME", ""))}


def graph_token(settings: Dict[str, str]) -> Tuple[str, str]:
    """(token, error) for the app registration, client-credentials flow."""
    if requests is None:
        return "", "the requests package is not installed"
    try:
        response = requests.post(
            f"{GRAPH_LOGIN}/{settings['tenant']}/oauth2/v2.0/token",
            data={"client_id": settings["client"],
                  "client_secret": settings["secret"],
                  "scope": "https://graph.microsoft.com/.default",
                  "grant_type": "client_credentials"},
            timeout=60,
        )
    except Exception as exc:  # noqa: BLE001
        return "", f"the token request failed: {exc}"
    if response.status_code != 200:
        # The body names the actual problem — a wrong secret, a missing
        # consent — and guessing from the status code alone wastes an hour.
        return "", (f"the token request returned HTTP {response.status_code}: "
                    f"{response.text[:300]}")
    token = (response.json() or {}).get("access_token", "")
    return (token, "") if token else ("", "the token response carried no token")


def find_workbook_graph(log: Any) -> Tuple[str, Optional[bytes], str]:
    """The newest matching email's workbook, over Microsoft Graph."""
    settings = graph_settings()
    token, error = graph_token(settings)
    if error:
        return "", None, error
    if not settings["mailbox"]:
        return "", None, ("GRAPH_MAILBOX is not set and SMTP_USERNAME is "
                          "empty, so there is no mailbox to read")

    headers = {"Authorization": f"Bearer {token}"}
    since = (datetime.now(timezone.utc) - timedelta(days=SEARCH_DAYS)
             ).strftime("%Y-%m-%dT%H:%M:%SZ")
    # $filter rather than $search, and no $orderby: Graph refuses to sort a
    # subject-filtered list, so the newest is picked here instead.
    url = (f"{GRAPH_ROOT}/users/{settings['mailbox']}/messages"
           f"?$filter=receivedDateTime ge {since}"
           f" and subject eq '{SUBJECT}'"
           "&$select=id,subject,receivedDateTime,hasAttachments&$top=50")
    try:
        response = requests.get(url, headers=headers, timeout=120)
    except Exception as exc:  # noqa: BLE001
        return "", None, f"the mailbox search failed: {exc}"
    if response.status_code == 403:
        return "", None, (
            "Graph refused the mailbox (HTTP 403). The app registration "
            "needs the Mail.Read application permission with admin consent, "
            "and the mailbox must be inside any application access policy "
            f"that is in force. Body: {response.text[:300]}")
    if response.status_code != 200:
        return "", None, (f"the mailbox search returned HTTP "
                          f"{response.status_code}: {response.text[:300]}")

    messages = (response.json() or {}).get("value", [])
    if not messages:
        log(f"  No email with subject {SUBJECT!r} in the last "
            f"{SEARCH_DAYS} day(s).")
        return "", None, ""
    messages.sort(key=lambda m: m.get("receivedDateTime", ""), reverse=True)

    for message in messages:
        if not message.get("hasAttachments"):
            continue
        got = requests.get(
            f"{GRAPH_ROOT}/users/{settings['mailbox']}/messages/"
            f"{message['id']}/attachments",
            headers=headers, timeout=120)
        if got.status_code != 200:
            log(f"  The attachments could not be read: HTTP {got.status_code}.")
            continue
        name, blob = _workbook_from_graph((got.json() or {}).get("value", []), log)
        if blob is not None:
            log(f"  {message.get('subject')!r} of "
                f"{message.get('receivedDateTime')}: {name} ({len(blob)} bytes).")
            return name, blob, ""
    return "", None, ""


def _workbook_from_graph(attachments: List[Dict[str, Any]],
                         log: Any) -> Tuple[str, Optional[bytes]]:
    """The Excel attachment, or the one inside a zipped attachment."""
    zips: List[Tuple[str, bytes]] = []
    for item in attachments:
        name = _text(item.get("name"))
        raw = item.get("contentBytes")
        if not name or not raw:
            continue
        try:
            payload = base64.b64decode(raw)
        except Exception:  # noqa: BLE001
            continue
        lowered = name.lower()
        if lowered.endswith(EXCEL_SUFFIXES):
            return name, payload
        if lowered.endswith(".zip"):
            zips.append((name, payload))
    for name, payload in zips:
        inner_name, inner = _excel_in_zip(payload, name, log)
        if inner is not None:
            return inner_name, inner
    return "", None


def _excel_in_zip(payload: bytes, name: str,
                  log: Any) -> Tuple[str, Optional[bytes]]:
    """The first Excel file inside a zip, by either route."""
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            for inner in archive.namelist():
                if inner.endswith("/") or "__MACOSX" in inner:
                    continue
                if inner.lower().endswith(EXCEL_SUFFIXES):
                    with archive.open(inner) as handle:
                        # The workbook's own name, not the zip's: it is what
                        # goes in fileName and what the same-day check reads.
                        return inner.rsplit("/", 1)[-1], handle.read()
    except zipfile.BadZipFile:
        log(f"  {name} is not a readable zip; skipped.")
    return "", None


def find_workbook(log: Any) -> Tuple[str, Optional[bytes], str]:
    """
    (file name, bytes, error) for the newest matching email's workbook.

    Graph where it is configured, IMAP otherwise.

    ("", None, "") means the mailbox was readable and held no such email,
    which is not an error: a day without the report is a normal day, and the
    run goes on without it.
    """
    if bucket_settings():
        log("  Collecting the workbook from the drop bucket.")
        return find_workbook_bucket(log)

    if graph_settings():
        log("  Reading the mailbox over Microsoft Graph.")
        return find_workbook_graph(log)

    username, password, error = credentials()
    if error:
        return "", None, error
    log(f"  Reading the mailbox over IMAP ({IMAP_HOST}).")

    try:
        box = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    except Exception as exc:  # noqa: BLE001
        return "", None, f"could not reach {IMAP_HOST}:{IMAP_PORT}: {exc}"

    try:
        try:
            box.login(username, password)
        except imaplib.IMAP4.error as exc:
            # The usual cause by far: IMAP basic auth disabled on the tenant.
            return "", None, (
                f"the mailbox refused the sign-in ({exc}). If SMTP sending "
                "works with the same password, IMAP is probably switched off "
                "for this mailbox and the workbook needs Microsoft Graph "
                "instead.")
        box.select(IMAP_FOLDER, readonly=True)

        since = (date.today().toordinal() - SEARCH_DAYS)
        since_text = date.fromordinal(since).strftime("%d-%b-%Y")
        # HEADER rather than SUBJECT: SUBJECT matches substrings in some
        # servers and whole words in others, and the subject is fixed.
        status, data = box.search(None, "SINCE", since_text,
                                  "HEADER", "Subject", SUBJECT)
        if status != "OK":
            return "", None, f"the mailbox search failed: {status}"
        ids = (data[0] or b"").split()
        if not ids:
            log(f"  No email with subject {SUBJECT!r} in the last "
                f"{SEARCH_DAYS} day(s).")
            return "", None, ""

        # Newest first: one email a day, and a re-send supersedes.
        for message_id in reversed(ids):
            status, raw = box.fetch(message_id, "(RFC822)")
            if status != "OK" or not raw or not raw[0]:
                continue
            message = email.message_from_bytes(raw[0][1])
            subject = _header(message.get("Subject"))
            if SUBJECT.lower() not in subject.lower():
                continue
            name, blob, why = _workbook_from(message, log)
            if blob is not None:
                log(f"  {subject!r} of {_header(message.get('Date'))}: "
                    f"{name} ({len(blob)} bytes).")
                return name, blob, ""
            log(f"  {subject!r}: {why}")
        return "", None, ""
    finally:
        try:
            box.logout()
        except Exception:  # noqa: BLE001
            pass


def _workbook_from(message: Any, log: Any) -> Tuple[str, Optional[bytes], str]:
    """The Excel attachment, out of the message or out of a zip inside it."""
    zips: List[Tuple[str, bytes]] = []
    for part in message.walk():
        name = _header(part.get_filename())
        if not name:
            continue
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        lowered = name.lower()
        if lowered.endswith(EXCEL_SUFFIXES):
            return name, payload, ""
        if lowered.endswith(".zip"):
            zips.append((name, payload))

    for name, payload in zips:
        inner_name, inner = _excel_in_zip(payload, name, log)
        if inner is not None:
            return inner_name, inner, ""
    return "", None, "no Excel attachment, in the message or in a zip"


def rows_from_workbook(blob: bytes, name: str, log: Any) -> List[Dict[str, str]]:
    """
    [{"shipment", "location", "oerodp"}] for the imko sheet's wanted rows.

    This is build_ko_monitoring.read_workbook's rules, applied to bytes rather
    than a path: the `imko` sheet and no other, and only the ub_imko codes
    this dashboard is about. The workbook has other sheets — `lost` carries a
    VCVAIC column of its own holding product category names — and an earlier
    version of this read every sheet with that heading, which put several
    hundred rows like "WALLETS AND SMALL LEATHER GOODS" onto the dashboard.

    The constants come from build_ko_monitoring rather than being repeated
    here, so the emailed workbook and an uploaded one are read by the same
    rules and cannot drift apart. Imported inside the function because
    build_ko_monitoring imports this module.
    """
    if load_workbook is None:
        log("  openpyxl is not installed, so the workbook cannot be read.")
        return []
    try:
        from build_ko_monitoring import (ALLOWED_IMKO, SHEET_NAME, XLSX_OERODP_COLUMN,
                                         XLSX_SHIPMENT_COLUMN, XLSX_UB_COLUMN,
                                         cell_text, normalise_heading, normalise_imko)
    except ImportError as exc:
        log(f"  the workbook rules could not be loaded: {exc}")
        return []

    try:
        book = load_workbook(io.BytesIO(blob), read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001
        log(f"  {name} could not be opened: {exc}")
        return []

    if SHEET_NAME not in book.sheetnames:
        # Named, never guessed at. A workbook that has changed shape is worth
        # a complaint; reading whatever sheet looks closest is how the
        # category names got in.
        log(f"  {name} has no {SHEET_NAME!r} sheet. Found: "
            + ", ".join(book.sheetnames) + ". Nothing read.")
        book.close()
        return []

    stream = book[SHEET_NAME].iter_rows(values_only=True)
    try:
        header = [cell_text(cell) for cell in next(stream)]
    except StopIteration:
        log(f"  {name}: the {SHEET_NAME!r} sheet is empty.")
        book.close()
        return []

    wanted = {XLSX_SHIPMENT_COLUMN: "shipment",
              XLSX_UB_COLUMN: "ub_imko",
              XLSX_OERODP_COLUMN: "oerodp"}
    index: Dict[str, int] = {}
    for position, heading in enumerate(header):
        for column, key in wanted.items():
            if normalise_heading(heading) == normalise_heading(column):
                index.setdefault(key, position)

    # oerodp is taken where the sheet has it and done without where it does
    # not: gxo_ko has no column for it, and the run rebuilds it anyway.
    for required in ("shipment", "ub_imko"):
        if required not in index:
            log(f"  {name}: the {SHEET_NAME!r} sheet has no "
                f"{XLSX_SHIPMENT_COLUMN if required == 'shipment' else XLSX_UB_COLUMN}"
                " column. Found: " + ", ".join(header) + ". Nothing read.")
            book.close()
            return []

    found: List[Dict[str, str]] = []
    seen = set()
    total = dropped = 0
    for row in stream:
        if row is None or all(cell is None for cell in row):
            continue
        total += 1
        code = normalise_imko(row[index["ub_imko"]]
                              if index["ub_imko"] < len(row) else "")
        if code not in ALLOWED_IMKO:
            dropped += 1
            continue
        shipment = cell_text(row[index["shipment"]]
                             if index["shipment"] < len(row) else "")
        if not shipment:
            continue
        key = shipment.upper()
        if key in seen:
            continue
        seen.add(key)
        found.append({
            "shipment": shipment,
            "location": code,
            "oerodp": cell_text(row[index["oerodp"]])
            if "oerodp" in index and index["oerodp"] < len(row) else "",
        })
    book.close()
    log(f"  {name}: {total} row(s) on {SHEET_NAME!r}, {len(found)} shipment(s) "
        f"after the {XLSX_UB_COLUMN} filter"
        + (f" ({dropped} dropped)" if dropped else "") + ".")
    return found


# ---------------------------------------------------------------------------
# The gxo_ko side
# ---------------------------------------------------------------------------
# Resolved case-insensitively, the same way ko_operational_table does it, so a
# feed that renames a column does not silently write nothing.
GXO_KO_WRITE_FIELDS = {
    "shipment": ("orderNumber", "order_number", "shipmentOrderNumber",
                 "shipment_order_number", "shipment"),
    "location": ("location", "ub_imko", "ubImko"),
    "reason": ("reason",),
    "import_date": ("importDate", "import_date"),
    "report_date": ("reportDate", "report_date"),
    "file_name": ("fileName", "file_name"),
}


def _param(name: str, value: Any) -> Any:
    """
    A query parameter typed from the value, not guessed from the column.

    gxo_ko's columns are not all the type their names suggest — reportDate is
    a STRING while importDate is a TIMESTAMP — so the type has to come from
    what the table actually handed back. Reading a value and writing it
    unchanged then works whatever the column turns out to be, and a feed that
    changes a column's type does not need a code change here.
    """
    from google.cloud import bigquery
    if isinstance(value, datetime):          # before date: datetime is a date
        kind = "TIMESTAMP"
    elif isinstance(value, date):
        kind = "DATE"
    elif isinstance(value, bool):            # before int: bool is an int
        kind = "BOOL"
    elif isinstance(value, int):
        kind = "INT64"
    elif isinstance(value, float):
        kind = "FLOAT64"
    else:
        kind = "STRING"
    return bigquery.ScalarQueryParameter(name, kind, value)


def latest_import(client: Any, table: str, columns: Dict[str, str],
                  log: Any) -> Dict[str, Any]:
    """
    The import every new row joins: its importDate, reportDate and fileName.

    One query, because all three come from the same latest import and asking
    separately could straddle two of them if an import landed in between.
    """
    report = (f"MAX(`{columns['report_date']}`)" if "report_date" in columns
              else "CAST(NULL AS DATE)")
    name = (f"MAX(`{columns['file_name']}`)" if "file_name" in columns
            else "CAST(NULL AS STRING)")
    sql = f"""
        SELECT
          `{columns['import_date']}` AS import_date,
          {report} AS report_date,
          {name}   AS file_name
        FROM `{table}`
        WHERE `{columns['import_date']}` = (
          SELECT MAX(`{columns['import_date']}`) FROM `{table}`
        )
        GROUP BY 1
    """
    try:
        rows = list(client.query(sql).result())
    except Exception as exc:  # noqa: BLE001
        log(f"  {table}: the latest import could not be read: {exc}")
        return {}
    if not rows:
        return {}
    row = rows[0]
    return {"import_date": row["import_date"], "report_date": row["report_date"],
            "file_name": _text(row["file_name"])}


def already_ingested_today(latest: Dict[str, Any]) -> bool:
    """
    Has today's workbook already gone in?

    True when the latest import is today's and came from an Excel file rather
    than a CSV. GXO send one workbook a day, so a second run on the same day
    has nothing to collect and can leave the mailbox alone. A CSV file name
    means the latest import was the other feed, and says nothing about
    whether the workbook has arrived.
    """
    stamp = latest.get("import_date")
    if stamp is None:
        return False
    if isinstance(stamp, str):
        # A text column, as reportDate turned out to be. Take the date off
        # the front rather than insisting on a shape.
        try:
            stamp = datetime.fromisoformat(stamp.strip().replace("Z", "+00:00"))
        except ValueError:
            return False
    day = stamp.date() if isinstance(stamp, datetime) else stamp
    if not isinstance(day, date) or day != datetime.now(timezone.utc).date():
        return False
    return _text(latest.get("file_name")).lower().endswith(EXCEL_SUFFIXES)


def existing_shipments(client: Any, table: str, columns: Dict[str, str],
                       import_date: Any, log: Any) -> set:
    """The shipment numbers this import already holds, upper-cased."""
    sql = f"""
        SELECT DISTINCT UPPER(TRIM(CAST(`{columns['shipment']}` AS STRING))) AS shipment
        FROM `{table}`
        WHERE `{columns['import_date']}` = @import_date
    """
    try:
        from google.cloud import bigquery  # local: the caller may have no BQ
        job = client.query(sql, job_config=bigquery.QueryJobConfig(
            query_parameters=[_param("import_date", import_date)]))
        return {str(row["shipment"]) for row in job.result() if row["shipment"]}
    except Exception as exc:  # noqa: BLE001
        log(f"  {table}: the import's shipments could not be read: {exc}")
        return set()


def insert(client: Any, table: str, columns: Dict[str, str],
           rows: List[Dict[str, str]], latest: Dict[str, Any],
           file_name: str, log: Any) -> int:
    """
    Add the workbook's shipments to the latest import. Returns how many.

    reportDate and importDate are the latest import's, not today's: these
    rows are part of that import, and giving them a stamp of their own would
    split one day's picture across two and make MAX(importDate) return half
    of it.
    """
    if not rows:
        return 0
    from google.cloud import bigquery

    into = [columns["shipment"], columns["import_date"]]
    if "location" in columns:
        into.append(columns["location"])
    if "reason" in columns:
        into.append(columns["reason"])
    if "report_date" in columns:
        into.append(columns["report_date"])
    if "file_name" in columns:
        into.append(columns["file_name"])

    values: List[str] = []
    params: List[Any] = [
        _param("import_date", latest.get("import_date")),
        _param("report_date", latest.get("report_date")),
        _param("file_name", file_name),
    ]
    for position, row in enumerate(rows):
        params.append(_param(f"s{position}", row["shipment"]))
        params.append(_param(f"l{position}", row.get("location", "")))
        cells = [f"@s{position}", "@import_date"]
        if "location" in columns:
            cells.append(f"@l{position}")
        if "reason" in columns:
            # Left empty on purpose: the reason is filled from the other
            # sources, and a made-up one here would outrank them.
            cells.append("CAST(NULL AS STRING)")
        if "report_date" in columns:
            cells.append("@report_date")
        if "file_name" in columns:
            cells.append("@file_name")
        values.append("(" + ", ".join(cells) + ")")

    sql = (f"INSERT INTO `{table}` (" + ", ".join(f"`{c}`" for c in into) + ")\n"
           "VALUES\n  " + ",\n  ".join(values))
    try:
        client.query(sql, job_config=bigquery.QueryJobConfig(
            query_parameters=params)).result()
    except Exception as exc:  # noqa: BLE001 - the run goes on without them
        log(f"  {table}: the workbook's rows could not be inserted: {exc}")
        return 0
    return len(rows)


def ingest(client: Any, log: Any, workbook: Optional[Tuple[str, bytes]] = None,
           search_mailbox: bool = True) -> Dict[str, Any]:
    """
    Put the day's backlog workbook into gxo_ko, from the mailbox or from hand.

    `workbook` is an uploaded one, which skips the mailbox entirely.
    `search_mailbox` False skips it too, for a run that has already collected
    today's.

    Returns {"inserted", "skipped", "file_name", "reason"} — reason being why
    nothing was inserted, where that is worth saying. Never raises: a mailbox
    that cannot be reached must not stop the rest of the run.
    """
    from ko_operational_table import GXO_KO_TABLE, _columns_of, WLFS_PROJECT
    out: Dict[str, Any] = {"inserted": 0, "skipped": 0, "file_name": "", "reason": ""}
    if client is None:
        out["reason"] = "no warehouse BigQuery client"
        return out

    columns = _columns_of(client, WLFS_PROJECT, "monitoring", "gxo_ko",
                          GXO_KO_WRITE_FIELDS, log)
    if "shipment" not in columns or "import_date" not in columns:
        out["reason"] = "gxo_ko has no shipment or importDate column"
        log(f"  {GXO_KO_TABLE}: {out['reason']}; the workbook is skipped.")
        return out

    latest = latest_import(client, GXO_KO_TABLE, columns, log)
    if not latest:
        out["reason"] = "gxo_ko has no import to join"
        log(f"  {GXO_KO_TABLE}: {out['reason']}; the workbook is skipped.")
        return out

    if workbook is None:
        if not search_mailbox:
            out["reason"] = "the mailbox was not searched"
            return out
        if already_ingested_today(latest):
            out["reason"] = "today's workbook is already in"
            log(f"  {GXO_KO_TABLE}: the latest import is today's "
                f"{latest['file_name']}, so the mailbox is left alone.")
            return out
        name, blob, error = find_workbook(log)
        if error:
            out["reason"] = error
            log(f"  The mailbox could not be searched: {error}")
            return out
        if blob is None:
            out["reason"] = "no email found"
            return out
        workbook = (name, blob)

    name, blob = workbook
    out["file_name"] = name
    rows = rows_from_workbook(blob, name, log)
    if not rows:
        out["reason"] = "the workbook held no shipments"
        return out

    known = existing_shipments(client, GXO_KO_TABLE, columns,
                               latest["import_date"], log)
    fresh = [row for row in rows if row["shipment"].upper() not in known]
    out["skipped"] = len(rows) - len(fresh)
    if not fresh:
        out["reason"] = "every shipment in the workbook is already in"
        log(f"  {GXO_KO_TABLE}: all {len(rows)} shipment(s) from {name} are "
            "already in this import; nothing added.")
        return out

    out["inserted"] = insert(client, GXO_KO_TABLE, columns, fresh, latest,
                             name, log)
    log(f"  {GXO_KO_TABLE}: {out['inserted']} shipment(s) added from {name}"
        + (f", {out['skipped']} already in" if out["skipped"] else "") + ".")
    return out
