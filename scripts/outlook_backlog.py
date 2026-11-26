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

Reading the mailbox is IMAP against outlook.office365.com, with the same
SMTP_USERNAME and SMTP_PASSWORD the reporting system sends with. If the
tenant has IMAP basic auth switched off, every call here fails the same way
and says so, and the run carries on without the workbook.
"""
from __future__ import annotations

import email
import imaplib
import io
import os
import re
import zipfile
from datetime import date, datetime, timezone
from email.header import decode_header, make_header
from typing import Any, Dict, List, Optional, Tuple

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

# The two columns wanted out of the workbook, however they are spelled.
VCVAIC_NAMES = ("vcvaic",)
UB_IMKO_NAMES = ("ub_imko", "ubimko", "ub imko")


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


def find_workbook(log: Any) -> Tuple[str, Optional[bytes], str]:
    """
    (file name, bytes, error) for the newest matching email's workbook.

    ("", None, "") means the mailbox was readable and held no such email,
    which is not an error: a day without the report is a normal day, and the
    run goes on without it.
    """
    username, password, error = credentials()
    if error:
        return "", None, error

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
        try:
            with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                for inner in archive.namelist():
                    # Skip the folder entries and the junk Windows adds.
                    if inner.endswith("/") or "__MACOSX" in inner:
                        continue
                    if inner.lower().endswith(EXCEL_SUFFIXES):
                        with archive.open(inner) as handle:
                            # The name that matters is the workbook's, not the
                            # zip's: it is what goes in fileName, and what a
                            # later run reads back to tell Excel from CSV.
                            return inner.rsplit("/", 1)[-1], handle.read(), ""
        except zipfile.BadZipFile:
            log(f"  {name} is not a readable zip; skipped.")
    return "", None, "no Excel attachment, in the message or in a zip"


def rows_from_workbook(blob: bytes, name: str, log: Any) -> List[Dict[str, str]]:
    """
    [{"shipment": VCVAIC, "location": ub_imko}] for every row that has both.

    Every sheet is looked at, and the header row is found rather than assumed:
    these workbooks carry a title or a blank line or two above the headings
    often enough that fixing on row 1 would read nothing.
    """
    if load_workbook is None:
        log("  openpyxl is not installed, so the workbook cannot be read.")
        return []
    try:
        book = load_workbook(io.BytesIO(blob), read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001
        log(f"  {name} could not be opened: {exc}")
        return []

    found: List[Dict[str, str]] = []
    seen = set()
    for sheet in book.worksheets:
        rows = sheet.iter_rows(values_only=True)
        vc = ub = None
        header_at = -1
        for position, row in enumerate(rows):
            if position > 20:  # a header this far down is not a header
                break
            lowered = [_text(cell).lower() for cell in row]
            for index, cell in enumerate(lowered):
                if cell in VCVAIC_NAMES:
                    vc = index
                if cell in UB_IMKO_NAMES:
                    ub = index
            if vc is not None:
                header_at = position
                break
        if vc is None:
            continue
        log(f"  {name}: sheet {sheet.title!r}, headings on row {header_at + 1}.")
        for row in rows:  # the iterator is already past the header
            shipment = _text(row[vc]) if vc < len(row) else ""
            location = _text(row[ub]) if ub is not None and ub < len(row) else ""
            if not shipment:
                continue
            key = shipment.upper()
            if key in seen:
                continue
            seen.add(key)
            found.append({"shipment": shipment, "location": location})
    book.close()
    log(f"  {name}: {len(found)} shipment(s) read.")
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
            query_parameters=[bigquery.ScalarQueryParameter(
                "import_date", "TIMESTAMP" if isinstance(import_date, datetime)
                else "DATE", import_date)]))
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
        bigquery.ScalarQueryParameter(
            "import_date",
            "TIMESTAMP" if isinstance(latest.get("import_date"), datetime) else "DATE",
            latest.get("import_date")),
        bigquery.ScalarQueryParameter(
            "report_date",
            "TIMESTAMP" if isinstance(latest.get("report_date"), datetime) else "DATE",
            latest.get("report_date")),
        bigquery.ScalarQueryParameter("file_name", "STRING", file_name),
    ]
    for position, row in enumerate(rows):
        params.append(bigquery.ScalarQueryParameter(
            f"s{position}", "STRING", row["shipment"]))
        params.append(bigquery.ScalarQueryParameter(
            f"l{position}", "STRING", row.get("location", "")))
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
