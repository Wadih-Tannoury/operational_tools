#!/usr/bin/env python3
"""
The KO operational table.

    `tlg-business-intelligence-prd`.til.ko_operational_table

is what the dashboard shows, kept between runs: one row per shipment, for good.
`scripts/build_ko_monitoring.py` writes it at the end of every run and the
ko-monitoring-app console reads it.

Three things happen on each run, in this order:

 1. **Upsert.** Every shipment of the new upload is merged on
    `shipment_order_number`. A shipment already in the table has its row
    updated — never a second row added.
 2. **`ko_fix_date` is kept.** Once a run has found a shipment fixed, that
    timestamp stays, even if a later run of the same shipment comes back KO.
    `COALESCE(T.ko_fix_date, S.ko_fix_date)` is the whole of it.
 3. **Resolve.** Any row still open whose shipment is *not* in the new upload
    gets `ko_resolved = TRUE` and the moment it happened. A shipment that
    comes back in a later upload is reopened: the merge sets `ko_resolved`
    back to FALSE.

The write goes through a staging table and one MERGE rather than a query per
shipment: four hundred shipments is four hundred queries otherwise, and the
MERGE is what makes "already there" and "new" the same code path.

Nothing here raises. A table that cannot be reached costs the run its history,
not its dataset: the CSV under ko_monitoring/output/ is written either way.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

try:
    from google.cloud import bigquery
except ImportError:  # pragma: no cover - reported by the caller
    bigquery = None  # type: ignore[assignment]

PROJECT = "tlg-business-intelligence-prd"
DATASET = "til"
TABLE = "ko_operational_table"
FQN = f"{PROJECT}.{DATASET}.{TABLE}"

# (BigQuery column, type, the dataset column it comes from)
#
# The two are now spelled the same: the dataset under output/, the dashboard
# and the table all use the column names below, so there is one name per thing
# rather than a mapping to keep in step. The third element stays so that a
# future divergence has somewhere to live.
COLUMNS: List[Tuple[str, str, str]] = [
    ("shipment_order_number", "STRING", "shipment_order_number"),
    ("oerodp", "STRING", "oerodp"),
    ("ub_imko", "STRING", "ub_imko"),
    ("logistic_order_number", "STRING", "logistic_order_number"),
    ("oms_shipment_number", "STRING", "oms_shipment_number"),
    ("order_submit_date", "DATE", "order_submit_date"),
    ("shipment_creation_date", "DATE", "shipment_creation_date"),
    ("action", "STRING", "action"),
    ("fulfillment_status", "STRING", "fulfillment_status"),
    ("location_provider", "STRING", "location_provider"),
    ("location_name", "STRING", "location_name"),
    ("ko_fixed", "BOOL", "ko_fixed"),
    ("ko_fix_date", "TIMESTAMP", "ko_fix_date"),
    ("last_tried_by_gxo_date", "TIMESTAMP", "last_tried_by_gxo_date"),
    ("gxo_result_code", "STRING", "gxo_result_code"),
    ("tried_by_gxo_after_the_fix", "BOOL", "tried_by_gxo_after_the_fix"),
    ("ko_reason", "STRING", "ko_reason"),
    ("ko_resolved", "BOOL", "ko_resolved"),
    ("ko_resolved_date", "TIMESTAMP", "ko_resolved_date"),
    ("source", "STRING", "source"),
    ("brand", "STRING", "brand"),
    ("channel", "STRING", "channel"),
    # Who owns the KO, and why the monitoring table says it is one. Both come
    # from til.prd_fulfillment_monitoring_table as they stand there.
    ("ko_source", "STRING", "ko_source"),
    ("blocking_reason", "STRING", "blocking_reason"),
    # What GXO has to do about it, worked out from the blocking reason and
    # from ko_fixed. Empty when there is nothing for the warehouse to do.
    ("action_needed_from_gxo", "STRING", "action_needed_from_gxo"),
    # Was this shipment in the latest workbook or the latest gxo_ko import?
    # Set by a pass of its own after every run that carried both, so a run
    # with no upload leaves it as it stands rather than clearing it.
    ("in_latest_gxo_input", "BOOL", "in_latest_gxo_input"),
    ("eligible_for_package_closure", "BOOL", "eligible_for_package_closure"),
    ("shipping_tracking_number", "STRING", "shipping_tracking_number"),
    ("return_tracking_number", "STRING", "return_tracking_number"),
    ("api_call_date", "TIMESTAMP", "api_call_date"),
    ("batch", "STRING", "batch"),
]

# Columns the merge owns rather than the dataset: they are about the row's life
# in the table, not about what the run found.
BOOKKEEPING = [
    ("row_insertion_date", "TIMESTAMP"),
    ("row_last_modified_date", "TIMESTAMP"),
    ("last_run", "STRING"),
    ("ko_resolved", "BOOL"),
    ("ko_resolved_date", "TIMESTAMP"),
]

DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%Y/%m/%d", "%d-%m-%Y", "%Y%m%d")


# ---------------------------------------------------------------------------
# Turning a dashboard row into a table row
# ---------------------------------------------------------------------------

def as_date(value: Any) -> Optional[str]:
    """A CSV date cell as an ISO date, or None when it is not one."""
    text = str(value or "").strip()
    if not text:
        return None
    text = text.split("T")[0].split(" ")[0]
    for pattern in DATE_FORMATS:
        try:
            return datetime.strptime(text, pattern).date().isoformat()
        except ValueError:
            continue
    return None


def as_timestamp(value: Any) -> Optional[str]:
    """An ISO timestamp as the worker writes it, or None."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat()


def as_bool(value: Any) -> Optional[bool]:
    """The worker writes yes/no, and an empty string when it never found out."""
    text = str(value or "").strip().lower()
    if text in ("yes", "true", "1", "y", "t"):
        return True
    if text in ("no", "false", "0", "n", "f"):
        return False
    return None


def as_int(value: Any) -> Optional[int]:
    text = str(value or "").strip()
    if not re.fullmatch(r"-?\d+", text):
        return None
    return int(text)


def as_string(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    return text or None


CONVERTERS = {
    "STRING": as_string, "DATE": as_date, "TIMESTAMP": as_timestamp,
    "BOOL": as_bool, "INT64": as_int,
}


def to_record(row: Dict[str, str]) -> Dict[str, Any]:
    """One dashboard row as the staging table wants it."""
    record: Dict[str, Any] = {}
    for column, kind, origin in COLUMNS:
        record[column] = CONVERTERS[kind](row.get(origin, ""))
    return record


def staging_schema() -> List[Any]:
    kinds = {"STRING": "STRING", "DATE": "DATE", "TIMESTAMP": "TIMESTAMP",
             "BOOL": "BOOLEAN", "INT64": "INTEGER"}
    return [
        bigquery.SchemaField(column, kinds[kind], mode="NULLABLE")
        for column, kind, _ in COLUMNS
    ]


# ---------------------------------------------------------------------------
# The write
# ---------------------------------------------------------------------------

def _merge_sql(staging: str) -> str:
    """
    The whole upsert.

    The key is the shipment number, trimmed and upper-cased on both sides, so
    the same shipment spelled with a stray space is still the same row rather
    than a second one.
    """
    updates = []
    for column, _, _ in COLUMNS:
        if column == "shipment_order_number":
            continue
        if column in ("ko_resolved", "ko_resolved_date"):
            # Set further down, to FALSE: an upload that carries a shipment
            # reopens it. Skipped here so the statement cannot assign the same
            # column twice, which BigQuery rejects outright.
            continue
        if column == "tried_by_gxo_after_the_fix":
            # FALSE until the warehouse is seen trying after the fix, and TRUE
            # for good once it has: the attempt happened, and a later run that
            # finds an older attempt cannot unsay it. Computed here rather than
            # taken from the upload so it can never disagree with the two dates
            # it is about.
            updates.append(
                "tried_by_gxo_after_the_fix = IFNULL(T.tried_by_gxo_after_the_fix, FALSE)"
                " OR (COALESCE(T.ko_fix_date, S.ko_fix_date) IS NOT NULL"
                " AND S.last_tried_by_gxo_date IS NOT NULL"
                " AND S.last_tried_by_gxo_date > COALESCE(T.ko_fix_date, S.ko_fix_date))"
            )
            continue
        if column == "ko_fixed":
            # Always derived from the date, so the two can never disagree —
            # whichever of the stored or the incoming date survives below.
            updates.append(
                "ko_fixed = COALESCE(T.ko_fix_date, S.ko_fix_date) IS NOT NULL"
            )
            continue
        if column == "ko_fix_date":
            # Once a shipment has been seen fixed, that is history: a later run
            # that comes back KO does not erase the date it was fixed on.
            updates.append("ko_fix_date = COALESCE(T.ko_fix_date, S.ko_fix_date)")
            continue
        updates.append(f"{column} = S.{column}")
    # The column being set is never qualified with the target alias: BigQuery
    # takes the left of a MERGE ... SET as a column of the target already, and
    # rejects some spellings that name it again. The right side still is.
    updates += [
        "row_last_modified_date = @now",
        "last_run = @run",
        # Back in the uploads means back on the dashboard.
        "ko_resolved = FALSE",
        "ko_resolved_date = CAST(NULL AS TIMESTAMP)",
    ]

    # Same two left out here, for the same reason: they are named once, at the
    # end, with the values a newly written row should have.
    inserted = [column for column, _, _ in COLUMNS
                if column not in ("ko_resolved", "ko_resolved_date")]
    insert_columns = inserted + [
        "row_insertion_date", "row_last_modified_date", "last_run",
        "ko_resolved", "ko_resolved_date",
    ]
    insert_values = [
        "S.ko_fix_date IS NOT NULL" if column == "ko_fixed"
        else ("IFNULL(S.tried_by_gxo_after_the_fix, FALSE)"
              if column == "tried_by_gxo_after_the_fix" else f"S.{column}")
        for column in inserted
    ] + [
        "@now", "@now", "@run", "FALSE", "CAST(NULL AS TIMESTAMP)",
    ]

    return f"""
        MERGE `{FQN}` AS T
        USING (
            SELECT * FROM `{staging}`
            WHERE shipment_order_number IS NOT NULL AND TRIM(shipment_order_number) != ''
        ) AS S
        ON UPPER(TRIM(T.shipment_order_number)) = UPPER(TRIM(S.shipment_order_number))
        WHEN MATCHED THEN UPDATE SET
            {", ".join(updates)}
        WHEN NOT MATCHED THEN INSERT ({", ".join(insert_columns)})
            VALUES ({", ".join(insert_values)})
    """


# Which shipments the latest workbook and the latest gxo_ko import carry.
# Run as one statement over the whole table after a run that had both, so a
# shipment that has dropped out of them flips to FALSE in the same pass that
# sets the ones still there to TRUE.
LATEST_INPUT_SQL = f"""
    UPDATE `{FQN}`
    SET in_latest_gxo_input = UPPER(TRIM(shipment_order_number)) IN UNNEST(@present),
        row_last_modified_date = @now
    WHERE shipment_order_number IS NOT NULL
"""


RESOLVE_SQL = f"""
    UPDATE `{FQN}`
    SET ko_resolved = TRUE,
        ko_resolved_date = @now,
        row_last_modified_date = @now,
        last_run = @run
    WHERE NOT IFNULL(ko_resolved, FALSE)
      AND UPPER(TRIM(shipment_order_number)) IN UNNEST(@resolved)
"""


def mark_latest_inputs(client: Any, present: Sequence[str], run: str,
                       log: Any) -> Tuple[int, str]:
    """
    Record which shipments the latest workbook and gxo_ko import carry.

    One statement over the whole table: the shipments in `present` get TRUE,
    every other row FALSE. Called only after a run that actually had both
    inputs — on a run without them the column is left exactly as it was, so
    "the latest upload" keeps meaning the latest upload rather than the latest
    run.
    """
    if bigquery is None or client is None:
        return 0, "no BigQuery client"
    numbers = sorted({str(number).strip().upper() for number in present
                      if str(number).strip()})
    try:
        job = client.query(LATEST_INPUT_SQL, job_config=bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ArrayQueryParameter("present", "STRING", numbers),
                bigquery.ScalarQueryParameter(
                    "now", "TIMESTAMP", datetime.now(timezone.utc)),
            ]))
        job.result()
    except Exception as exc:  # noqa: BLE001 - the dashboards fall back on the flag
        log(f"  in_latest_gxo_input could not be set: {exc}")
        return 0, str(exc)
    touched = job.num_dml_affected_rows or 0
    log(f"  in_latest_gxo_input: {len(numbers)} shipment(s) in the latest "
        f"inputs, {touched} row(s) updated.")
    return touched, ""


def resolve_many(client: Any, shipments: Sequence[str], run: str,
                 log: Any) -> Tuple[int, str]:
    """
    Mark these shipments resolved, in one statement.

    Which shipments those are is decided in the worker, because the rule
    depends on who holds the shipment and on two tables in another project.
    This only writes the answer.
    """
    if bigquery is None or client is None:
        return 0, "no BigQuery client"
    numbers = sorted({str(number).strip().upper() for number in shipments if number})
    if not numbers:
        return 0, ""
    try:
        job = client.query(RESOLVE_SQL, job_config=bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("now", "TIMESTAMP", datetime.now(timezone.utc)),
                bigquery.ScalarQueryParameter("run", "STRING", run),
                bigquery.ArrayQueryParameter("resolved", "STRING", numbers),
            ]))
        job.result()
        return job.num_dml_affected_rows or 0, ""
    except Exception as exc:  # noqa: BLE001
        log(f"  Could not mark shipments resolved: {exc}")
        return 0, str(exc)


def write(
    client: Any,
    rows: Sequence[Dict[str, str]],
    run: str,
    log: Any,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    Upsert this run's shipments into the table.

    Resolving is a separate pass — see resolve_many — because the rule depends
    on who holds the shipment and on two tables in the warehouse's own project,
    not on what this upload happened to contain.

    Returns a summary; nothing raises.
    """
    outcome: Dict[str, Any] = {
        "written": 0, "resolved": 0, "before": None, "after": None, "error": "",
    }
    if bigquery is None:
        outcome["error"] = "google-cloud-bigquery is not installed"
        return outcome
    if client is None:
        outcome["error"] = "no BigQuery client"
        return outcome

    records = [to_record(row) for row in rows]
    records = [record for record in records if record.get("shipment_order_number")]
    if not records:
        outcome["error"] = "nothing to write"
        return outcome

    if dry_run:
        log(f"  Dry run: {len(records)} row(s) would be merged into {TABLE}.")
        outcome["written"] = len(records)
        return outcome

    now = datetime.now(timezone.utc)
    staging_id = f"{PROJECT}.{DATASET}._ko_monitoring_staging_{run.replace('-', '_')}"

    try:
        # 1. the upload, into a staging table of its own
        job_config = bigquery.LoadJobConfig(
            schema=staging_schema(),
            write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
        )
        client.load_table_from_json(records, staging_id, job_config=job_config).result()

        # 2. one MERGE for the whole batch
        parameters = [
            bigquery.ScalarQueryParameter("now", "TIMESTAMP", now),
            bigquery.ScalarQueryParameter("run", "STRING", run),
        ]
        merge = client.query(
            _merge_sql(staging_id),
            job_config=bigquery.QueryJobConfig(query_parameters=parameters),
        )
        merge.result()
        outcome["written"] = merge.num_dml_affected_rows or len(records)

    except Exception as exc:  # noqa: BLE001 - the dataset is written either way
        outcome["error"] = str(exc)
        log(f"  {FQN} could not be written: {exc}")
    finally:
        try:
            client.delete_table(staging_id, not_found_ok=True)
        except Exception:  # noqa: BLE001 - a leftover staging table is not worth a failure
            pass

    if not outcome["error"]:
        log(f"  {FQN}: {outcome['written']} row(s) merged.")
    return outcome


# What is carried forward untouched when a shipment is already fixed: the
# evidence of how it was fixed. Everything else — the CSV columns, OERODP, the
# warehouse attempt — is refreshed from this run, because that is current.
CARRY_FORWARD = [
    "ko_fix_date", "eligible_for_package_closure",
    "label_shplbl_populated", "label_shplbl_format", "label_retlbl_present",
    "label_retlbl_populated", "label_retlbl_format",
    "shipping_tracking_number", "return_tracking_number",
    "doc_inv_populated", "doc_rpi_populated", "api_call_date",
]


def _as_text(value: Any, kind: str) -> str:
    """A table value back in the spelling the dashboard rows use."""
    if value is None:
        return ""
    if kind == "BOOL":
        return "yes" if value else "no"
    if kind == "TIMESTAMP":
        if isinstance(value, datetime):
            moment = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
            return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return str(value)
    if kind == "DATE":
        return value.isoformat() if isinstance(value, date) else str(value)
    return str(value)


def fetch_fixed(
    client: Any, shipments: Sequence[str], log: Any,
) -> Dict[str, Dict[str, str]]:
    """
    The shipments this table already records as fixed, with the evidence.

    Keyed by the shipment number, trimmed and upper-cased, with the dashboard's
    own column names — so the caller can drop the values straight onto its row
    and leave the documents API alone.
    """
    if bigquery is None or client is None or not shipments:
        return {}
    kinds = {column: kind for column, kind, _ in COLUMNS}
    headings = {column: heading for column, _, heading in COLUMNS}
    wanted = [column for column in CARRY_FORWARD if column in kinds]
    numbers = sorted({str(number).strip().upper() for number in shipments if number})

    sql = f"""
        SELECT UPPER(TRIM(shipment_order_number)) AS key, {', '.join(wanted)}
        FROM `{FQN}`
        WHERE IFNULL(ko_fixed, FALSE)
          AND UPPER(TRIM(shipment_order_number)) IN UNNEST(@numbers)
    """
    try:
        job = client.query(sql, job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ArrayQueryParameter("numbers", "STRING", numbers),
        ]))
        rows = list(job.result())
    except Exception as exc:  # noqa: BLE001 - a failed lookup just means asking again
        log(f"  Could not read what is already fixed: {exc}")
        return {}

    found: Dict[str, Dict[str, str]] = {}
    for row in rows:
        values = {
            headings[column]: _as_text(row[column], kinds[column])
            for column in wanted
        }
        found[str(row["key"])] = values
    return found


MONITORING_TABLE = f"{PROJECT}.{DATASET}.prd_fulfillment_monitoring_table"
# Where a resolved shipment goes. Same schema as the operational table plus
# the two columns that say when it was archived, created from it with
# CREATE TABLE LIKE so the two can never drift apart.
HISTORY_TABLE = "ko_operational_history_table"
HISTORY_FQN = f"{PROJECT}.{DATASET}.{HISTORY_TABLE}"
# Blocking reasons that are not this workflow's business. A row carrying one
# of these is never taken into the operational table:
#
#   KO fixed, already shipped   the shipment left the warehouse — the opposite
#                               of a KO
#   Shipment packed at GXO ...  the warehouse has done its part; what is stuck
#                               is the OMS status, not the documents
#   GXO shared the wrong ...    a stock disagreement, settled between the
#                               warehouse and the inventory app, not by
#                               re-asking for documents
#
# None of them can be fixed by the documents API, so a shipment carrying one
# would sit in the table being re-asked for ever.
EXCLUDED_REASONS = (
    "KO fixed, already shipped",
    "Shipment packed at GXO but not shipped and still READY in OMS",
    "GXO shared the wrong quantity",
)

# The warehouse's own project, read with WLFS_BIGQUERY_CREDS.
WLFS_PROJECT = "tlg-wlfs-prd"
GXO_KO_TABLE = f"{WLFS_PROJECT}.monitoring.gxo_ko"
SHIPMENT_STATUS_TABLE = f"{WLFS_PROJECT}.til.prd_shipmentStatus"

SHIPPED_EVENT_TYPES = ("SHIPPED",)
# Spelled both ways across the feeds, so both are asked for.
CANCELLED_EVENT_TYPES = ("CANCELLED", "CANCELED")

# prd_shipmentStatus and gxo_ko column names, resolved case-insensitively.
STATUS_FIELDS = {
    "shipment": ("shipmentOrderNumber", "shipment_order_number"),
    "logistic": ("logisticOrderNumber", "logistic_order_number"),
    "event": ("eventType", "event_type"),
    "payload": ("requestPayload", "request_payload", "payload", "eventPayload"),
}

# Where the logistics order number hides in a SHIPPED row. The table carries
# the event's own request body, and for GXO the number is only in there:
#   {"data": {"logisticOrderNumber": "OW110717", ...}, ...}
# A regular expression rather than JSON_VALUE on purpose — the column is a
# string, and a row whose body is truncated or not valid JSON should cost that
# row its number, not the whole query an error.
PAYLOAD_LOGISTIC_PATTERN = r'"logisticOrderNumber"\s*:\s*"([^"]*)"'
GXO_KO_FIELDS = {
    # The warehouse file calls the shipment number orderNumber — the same
    # spelling til.prd_fulfillment_monitoring_table reads it by. The others
    # are kept in case the feed is ever renamed.
    "shipment": ("orderNumber", "order_number", "shipmentOrderNumber",
                 "shipment_order_number", "shipment"),
    "logistic": ("logisticOrderNumber", "logistic_order_number"),
    "import_date": ("importDate", "import_date"),
}


def _columns_of(client: Any, project: str, dataset: str, table: str,
                fields: Dict[str, Tuple[str, ...]], log: Any) -> Dict[str, str]:
    """Which spelling of each field the table actually has."""
    sql = f"""
        SELECT column_name
        FROM `{project}.{dataset}`.INFORMATION_SCHEMA.COLUMNS
        WHERE table_name = @table
    """
    try:
        job = client.query(sql, job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("table", "STRING", table),
        ]))
        present = {str(row["column_name"]).lower(): str(row["column_name"])
                   for row in job.result()}
    except Exception as exc:  # noqa: BLE001
        log(f"  Could not read the schema of {project}.{dataset}.{table}: {exc}")
        return {}
    found: Dict[str, str] = {}
    for logical, candidates in fields.items():
        for candidate in candidates:
            if candidate.lower() in present:
                found[logical] = present[candidate.lower()]
                break
    return found


def fetch_gxo_ko(client: Any, log: Any) -> Dict[str, str]:
    """
    The warehouse's own KO list, as of its latest import.

        SELECT * FROM `tlg-wlfs-prd`.monitoring.gxo_ko
        WHERE importDate = (SELECT MAX(importDate) FROM the same)

    Returns {normalised shipment number: logistics order number}, the second
    empty when the table does not carry one. These shipments are GXO's, so a
    shipment here that neither file mentions still belongs on the dashboard.
    """
    if bigquery is None or client is None:
        return {}
    columns = _columns_of(client, WLFS_PROJECT, "monitoring", "gxo_ko", GXO_KO_FIELDS, log)
    if "shipment" not in columns or "import_date" not in columns:
        log(f"  {GXO_KO_TABLE}: no shipment or importDate column; skipped.")
        return {}

    logistic = (f"CAST(`{columns['logistic']}` AS STRING)" if "logistic" in columns
                else "CAST(NULL AS STRING)")
    sql = f"""
        SELECT
          UPPER(TRIM(CAST(`{columns['shipment']}` AS STRING))) AS shipment,
          {logistic} AS logistic
        FROM `{GXO_KO_TABLE}`
        WHERE `{columns['import_date']}` = (
          SELECT MAX(`{columns['import_date']}`) FROM `{GXO_KO_TABLE}`
        )
    """
    try:
        rows = list(client.query(sql).result())
    except Exception as exc:  # noqa: BLE001 - the run goes on with the two files
        log(f"  The {GXO_KO_TABLE} read failed: {exc}")
        return {}

    found: Dict[str, str] = {}
    for row in rows:
        shipment = str(row["shipment"] or "").strip()
        if shipment:
            found.setdefault(shipment, str(row["logistic"] or "").strip())
    log(f"  {GXO_KO_TABLE}: {len(found)} shipment(s) at the latest import.")
    return found


def fetch_status_events(client: Any, keys: Sequence[Tuple[str, str]],
                        log: Any) -> Optional[Dict[str, Any]]:
    """
    The SHIPPED and CANCELLED events of these shipments, in one read.

    Returns {"shipped": {(shipment, logistic), ...},
             "cancelled": {...},
             "paired": bool}

    `paired` says whether the logistics order number could be read at all. It
    normally can — from its own column, or from the event's requestPayload,
    which is where GXO's rows carry it. When it cannot, the pairs come back
    with an empty logistics component and the caller has to decide whether a
    shipment-only match is good enough for what it is doing.

    Returns None when the table could not be read. None is not an empty
    result: no event found means "not yet", a failed read means "unknown", and
    the resolve pass archives on neither.
    """
    if bigquery is None or client is None or not keys:
        return None
    columns = _columns_of(client, WLFS_PROJECT, "til", "prd_shipmentStatus",
                          STATUS_FIELDS, log)
    if "shipment" not in columns or "event" not in columns:
        log(f"  {SHIPMENT_STATUS_TABLE}: no shipment or eventType column; skipped.")
        return None

    parts = []
    if "logistic" in columns:
        parts.append(f"NULLIF(TRIM(CAST(`{columns['logistic']}` AS STRING)), '')")
    if "payload" in columns:
        parts.append(
            f"NULLIF(REGEXP_EXTRACT(CAST(`{columns['payload']}` AS STRING), "
            f"r'{PAYLOAD_LOGISTIC_PATTERN}'), '')"
        )
    paired = bool(parts)
    if not paired:
        log(f"  {SHIPMENT_STATUS_TABLE} has neither a logistics order column nor a "
            "request payload; events are matched on the shipment number alone.")
        logistic = "''"
    else:
        logistic = f"UPPER(IFNULL(COALESCE({', '.join(parts)}), ''))"
        where = ("its own column, then the request payload" if len(parts) == 2
                 else "its own column" if "logistic" in columns
                 else "the request payload")
        log(f"  {SHIPMENT_STATUS_TABLE}: the logistics order number is read from "
            f"{where}.")

    numbers = sorted({shipment for shipment, _ in keys if shipment})
    wanted = list(SHIPPED_EVENT_TYPES) + list(CANCELLED_EVENT_TYPES)
    sql = f"""
        SELECT DISTINCT
          UPPER(TRIM(CAST(`{columns['shipment']}` AS STRING))) AS shipment,
          {logistic} AS logistic,
          UPPER(TRIM(CAST(`{columns['event']}` AS STRING))) AS event_type
        FROM `{SHIPMENT_STATUS_TABLE}`
        WHERE UPPER(TRIM(CAST(`{columns['shipment']}` AS STRING))) IN UNNEST(@numbers)
          AND UPPER(TRIM(CAST(`{columns['event']}` AS STRING))) IN UNNEST(@types)
    """
    try:
        job = client.query(sql, job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ArrayQueryParameter("numbers", "STRING", numbers),
            bigquery.ArrayQueryParameter("types", "STRING", wanted),
        ]))
        rows = list(job.result())
    except Exception as exc:  # noqa: BLE001
        log(f"  The {SHIPMENT_STATUS_TABLE} read failed: {exc}")
        return None

    found: Dict[str, Any] = {"shipped": set(), "cancelled": set(), "paired": paired}
    for row in rows:
        pair = (str(row["shipment"]), str(row["logistic"]))
        if str(row["event_type"]) in SHIPPED_EVENT_TYPES:
            found["shipped"].add(pair)
        else:
            found["cancelled"].add(pair)
    log(f"  {SHIPMENT_STATUS_TABLE}: of {len(numbers)} shipment(s), "
        f"{len({s for s, _ in found['shipped']})} have a SHIPPED event and "
        f"{len({s for s, _ in found['cancelled']})} a CANCELLED one.")
    return found


def has_event(events: Dict[str, Any], kind: str, shipment: str, logistic: str) -> bool:
    """
    Does this shipment have an event of that kind, matched on both numbers?

    The logistics order number has to agree, because a shipmentOrderNumber can
    be reused: an event belonging to an earlier instance of the same number
    must not settle the one that is open now. The exception is a feed that
    carries no logistics order number at all, where the shipment number is all
    there is to match on and the log has already said so.
    """
    pairs = events.get(kind, set())
    if not events.get("paired"):
        return any(number == shipment for number, _ in pairs)
    return (shipment, logistic) in pairs


def fetch_unresolved(client: Any, log: Any) -> List[Dict[str, str]]:
    """Every row not yet resolved, fixed ones included — the resolve pass reads this."""
    return _read_rows(client, log, where="NOT IFNULL(ko_resolved, FALSE)",
                      limit=0, what="not resolved yet")

# What the monitoring table is read for, and the spellings each field may have
# there. The first name that exists is used, so a renamed column costs a line
# here rather than a failed run.
MONITORING_FIELDS = {
    "shipment": ("shipment_order_number", "shipmentOrderNumber", "shipment_number",
                 "shipment"),
    "external_error_message": ("external_error_message", "externalErrorMessage"),
    "error_message": ("error_message", "errorMessage"),
    "blocking_reason": ("blocking_reason", "blockingReason"),
    "location_provider": ("location_provider", "locationProvider"),
    "location_name": ("location_name", "locationName", "location"),
    "channel": ("channel",),
    "ko_source": ("ko_source", "koSource"),
}


def monitoring_columns(client: Any, log: Any) -> Dict[str, str]:
    """
    Which of the fields above the monitoring table actually has.

    Read from INFORMATION_SCHEMA rather than assumed, because this worker does
    not own that table: a column renamed there should cost this run the one
    field, not the whole lookup.
    """
    sql = f"""
        SELECT column_name
        FROM `{PROJECT}.{DATASET}`.INFORMATION_SCHEMA.COLUMNS
        WHERE table_name = 'prd_fulfillment_monitoring_table'
    """
    try:
        present = {str(row["column_name"]).lower() for row in client.query(sql).result()}
    except Exception as exc:  # noqa: BLE001
        log(f"  Could not read {MONITORING_TABLE}: {exc}")
        return {}
    if not present:
        log(f"  {MONITORING_TABLE} does not exist, or cannot be read.")
        return {}

    found: Dict[str, str] = {}
    for logical, candidates in MONITORING_FIELDS.items():
        for candidate in candidates:
            if candidate.lower() in present:
                found[logical] = candidate
                break
    missing = [name for name in MONITORING_FIELDS if name not in found]
    if missing:
        log(f"  {MONITORING_TABLE} has no " + ", ".join(missing)
            + "; those stay empty.")
    return found


def fetch_monitoring(
    client: Any, shipments: Sequence[str], log: Any,
) -> Dict[str, Dict[str, str]]:
    """
    The monitoring table's say on why a shipment is stuck, and whose warehouse
    it is in.

    Keyed by the shipment number, trimmed and upper-cased. The caller decides
    what to do with the three error fields; this only reads them.
    """
    if bigquery is None or client is None or not shipments:
        return {}
    columns = monitoring_columns(client, log)
    if "shipment" not in columns:
        return {}

    wanted = [name for name in
              ("external_error_message", "error_message", "blocking_reason",
               "location_provider", "location_name")
              if name in columns]
    if not wanted:
        return {}

    selects = ", ".join(f"CAST(`{columns[name]}` AS STRING) AS {name}" for name in wanted)
    numbers = sorted({str(number).strip().upper() for number in shipments if number})
    sql = f"""
        SELECT UPPER(TRIM(CAST(`{columns['shipment']}` AS STRING))) AS key, {selects}
        FROM `{MONITORING_TABLE}`
        WHERE UPPER(TRIM(CAST(`{columns['shipment']}` AS STRING))) IN UNNEST(@numbers)
    """
    try:
        job = client.query(sql, job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ArrayQueryParameter("numbers", "STRING", numbers),
        ]))
        rows = list(job.result())
    except Exception as exc:  # noqa: BLE001 - the run goes on without it
        log(f"  The {MONITORING_TABLE} lookup failed: {exc}")
        return {}

    found: Dict[str, Dict[str, str]] = {}
    for row in rows:
        # One row per shipment is expected; where there are several the first
        # that carries a value for a field wins, so nothing already found is
        # overwritten with a blank.
        entry = found.setdefault(str(row["key"]), {})
        for name in wanted:
            value = str(row[name] or "").strip()
            if value and not entry.get(name):
                entry[name] = value
    log(f"  {MONITORING_TABLE}: {len(found)} of {len(numbers)} shipment(s) matched.")
    return found


def already_known(client: Any, numbers: Sequence[str], log: Any) -> set:
    """
    Which of these shipments the operational or history table already holds.

    Used before taking a shipment in from gxo_ko on a run with no upload: the
    merge would only rewrite what is there, and a shipment already archived
    should not be reopened by a warehouse file that still lists it.
    """
    wanted = sorted({str(number).strip().upper() for number in numbers
                     if str(number).strip()})
    if bigquery is None or client is None or not wanted:
        return set()
    sources = [f"SELECT shipment_order_number FROM `{FQN}`"]
    if _table_exists(client, HISTORY_TABLE, log):
        sources.append(f"SELECT shipment_order_number FROM `{HISTORY_FQN}`")
    sql = f"""
        SELECT DISTINCT UPPER(TRIM(shipment_order_number)) AS shipment
        FROM ({' UNION ALL '.join(sources)})
        WHERE UPPER(TRIM(shipment_order_number)) IN UNNEST(@wanted)
    """
    try:
        job = client.query(sql, job_config=bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ArrayQueryParameter("wanted", "STRING", wanted)]))
        return {str(row["shipment"]) for row in job.result()}
    except Exception as exc:  # noqa: BLE001 - better to re-add than to lose one
        log(f"  Could not check which shipments are already known: {exc}")
        return set()


def update_columns(client: Any, rows: Sequence[Dict[str, str]],
                   columns: Sequence[str], run: str, log: Any,
                   table: str = TABLE, dry_run: bool = False) -> Dict[str, Any]:
    """
    Write a few named columns onto rows that already exist, and nothing else.

    The backfill's writer. The MERGE in write() is for a run that has decided
    something: it stamps last_run, carries the verdict columns and inserts
    shipments it has not seen before. A backfill decides nothing, and it also
    has to write to the archive, where an INSERT would resurrect a shipment
    that was deliberately taken out of the operational table. So this updates
    in place, matched on the shipment number, and a shipment the table does
    not have is simply not written.

    Only the named columns move, and only where the backfill found something:
    COALESCE keeps the stored value when the new one is null, so a lookup that
    came back empty never blanks a column that was already filled in.
    row_last_modified_date is stamped because something did change; last_run is
    not, because no run decided anything here.
    """
    outcome: Dict[str, Any] = {"updated": 0, "error": ""}
    if bigquery is None or client is None or not rows or not columns:
        return outcome

    fqn = f"{PROJECT}.{DATASET}.{table}"
    live = set(_live_columns(client, log, table=table))
    if not live:
        outcome["error"] = f"{table} could not be read"
        return outcome
    writable = [column for column in columns if column in live]
    missing = [column for column in columns if column not in live]
    if missing:
        log(f"  {table} has no {', '.join(missing)}; those are not written.")
    if not writable:
        return outcome

    records = [to_record(row) for row in rows]
    records = [record for record in records if record.get("shipment_order_number")]
    if not records:
        return outcome

    if dry_run:
        log(f"  Dry run: {len(records)} row(s) of {table} would have "
            f"{', '.join(writable)} filled in.")
        return outcome

    staging_id = f"{PROJECT}.{DATASET}._ko_backfill_staging_{run.replace('-', '_')}"
    assignments = ",\n          ".join(
        f"`{column}` = COALESCE(source.`{column}`, target.`{column}`)"
        for column in writable
    )
    sql = f"""
        UPDATE `{fqn}` AS target
        SET
          {assignments},
          row_last_modified_date = CURRENT_TIMESTAMP()
        FROM `{staging_id}` AS source
        WHERE UPPER(TRIM(target.shipment_order_number))
            = UPPER(TRIM(source.shipment_order_number))
    """
    try:
        job_config = bigquery.LoadJobConfig(
            schema=staging_schema(),
            write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
        )
        client.load_table_from_json(records, staging_id, job_config=job_config).result()
        job = client.query(sql)
        job.result()
        outcome["updated"] = job.num_dml_affected_rows or 0
    except Exception as exc:  # noqa: BLE001 - the run reports it and carries on
        outcome["error"] = str(exc)
        log(f"  {table} could not be updated: {exc}")
        return outcome
    finally:
        try:
            client.delete_table(staging_id, not_found_ok=True)
        except Exception:  # noqa: BLE001 - a staging table left behind is harmless
            pass

    log(f"  {fqn}: {outcome['updated']} row(s) updated ({', '.join(writable)}).")
    return outcome


def fetch_all(client: Any, log: Any, limit: int = 0) -> List[Dict[str, str]]:
    """
    Every row of the table, resolved ones included.

    For the backfill pass, which fills in what can be filled in rather than
    deciding anything: a shipment that dropped out of the uploads months ago
    still deserves its ko_reason and its location_provider.
    """
    return _read_rows(client, log, where="TRUE", limit=limit, what="row(s) in all")


def fetch_all_history(client: Any, log: Any, limit: int = 0) -> List[Dict[str, str]]:
    """
    Every row of the archive, for the backfill.

    An archived shipment is still read: the dashboard does not show it, but
    the history table is what anyone looks at to ask what happened to a
    shipment, and a row archived before a column existed answers that question
    worse than one that was filled in. Nothing about its verdict changes —
    only the descriptive columns are written back.
    """
    return _read_rows(client, log, where="TRUE", limit=limit,
                      what="row(s) in the archive", table=HISTORY_TABLE)


def fetch_open(client: Any, log: Any, limit: int = 0) -> List[Dict[str, str]]:
    """
    The shipments still worth asking about: not fixed, and not resolved.

    A shipment that is fixed has nothing left to find out, and one that is
    resolved has dropped out of the uploads, so neither is asked again.
    """
    return _read_rows(
        client, log,
        where="NOT IFNULL(ko_fixed, FALSE) AND NOT IFNULL(ko_resolved, FALSE)",
        limit=limit, what="still open and not fixed",
    )


def _read_rows(client: Any, log: Any, where: str, limit: int,
               what: str, table: str = TABLE) -> List[Dict[str, str]]:
    """Rows of the table in the dataset's own column names."""
    if bigquery is None or client is None:
        return []
    fqn = f"{PROJECT}.{DATASET}.{table}"
    kinds = {column: kind for column, kind, _ in COLUMNS}
    headings = {column: heading for column, _, heading in COLUMNS}
    columns = [column for column, _, _ in COLUMNS]
    if table != TABLE:
        # The archive carries the same columns plus archived_at and
        # archived_run, but it can lag a migration, so only ask for what it
        # actually has rather than failing the whole read on one column.
        live = set(_live_columns(client, log, table=table))
        if live:
            columns = [column for column in columns if column in live]

    sql = f"""
        SELECT {', '.join(f'`{column}`' for column in columns)}
        FROM `{fqn}`
        WHERE ({where})
          AND shipment_order_number IS NOT NULL
          AND TRIM(shipment_order_number) != ''
        ORDER BY row_last_modified_date DESC
        {f'LIMIT {int(limit)}' if limit and limit > 0 else ''}
    """
    try:
        rows = list(client.query(sql).result())
    except Exception as exc:  # noqa: BLE001 - a run with nothing to re-check is still a run
        log(f"  Could not read the table: {exc}")
        return []

    found = [
        {headings[column]: _as_text(row[column], kinds[column]) for column in columns}
        for row in rows
    ]
    log(f"  {fqn}: {len(found)} shipment(s) {what}.")
    return found


def open_count(client: Any, log: Any) -> Optional[int]:
    """How many rows are still open, for the run log. None when unknown."""
    if bigquery is None or client is None:
        return None
    try:
        job = client.query(
            f"SELECT COUNT(*) AS open_rows FROM `{FQN}` "
            "WHERE NOT IFNULL(ko_resolved, FALSE)"
        )
        for row in job.result():
            return int(row["open_rows"])
    except Exception as exc:  # noqa: BLE001
        log(f"  Could not count the open rows: {exc}")
    return None


# ---------------------------------------------------------------------------
# The monitoring table: taking its KO shipments
# ---------------------------------------------------------------------------

# What a monitoring row can hand over to a new operational row. The names are
# resolved from INFORMATION_SCHEMA like every other read of this table, so a
# column renamed there costs this run that one field rather than the lookup.
CANDIDATE_FIELDS = {
    "shipment": ("shipment_order_number", "shipmentOrderNumber"),
    "blocking_reason": ("blocking_reason", "blockingReason"),
    "logistic_order_number": ("logistic_order_number", "logisticOrderNumber"),
    "oms_shipment_number": ("oms_shipment_number", "omsShipmentNumber"),
    "order_submit_date": ("order_submit_date", "orderSubmitDate"),
    "shipment_creation_date": ("shipment_creation_date", "shipmentCreationDate"),
    "fulfillment_status": ("fulfillment_status", "fulfillmentStatus"),
    "location_provider": ("location_provider", "locationProvider"),
    "brand": ("brand",),
    "channel": ("channel",),
    "ko_source": ("ko_source", "koSource"),
    "shipment_status": ("shipmentStatus", "shipment_status"),
}

# Shipment statuses that never become a KO here. A BACKORDER or CUSTOMER_CARE
# shipment is diagnosed on the stock picture it was taken against, not on the
# warehouse's documents, so there is nothing for this workflow to re-try: it
# would sit in the table being asked about for ever.
EXCLUDED_STATUSES = ("BACKORDER", "CUSTOMER_CARE")


def fetch_monitoring_open(client: Any, log: Any) -> Optional[set]:
    """
    Every shipment the monitoring table still reports as blocked, by the same
    filters fetch_ko_candidates applies — the filtered view of that table.

    This is what a non-GXO shipment is resolved against: once it is no longer
    in here, the monitoring table has stopped calling it blocked and there is
    nothing left for this workflow to do with it.

    Returns None when the table could not be read. None is not an empty set:
    an empty set would resolve every non-GXO shipment at once, so the caller
    treats None as "ask again next run".
    """
    if bigquery is None or client is None:
        return None
    columns = _columns_of(client, PROJECT, DATASET,
                          "prd_fulfillment_monitoring_table", CANDIDATE_FIELDS, log)
    if "shipment" not in columns or "blocking_reason" not in columns:
        log(f"  {MONITORING_TABLE}: no shipment or blocking_reason column; "
            "no non-GXO shipment is resolved.")
        return None
    status = ""
    if "shipment_status" in columns:
        status = f"""
          AND UPPER(TRIM(IFNULL(CAST(`{columns['shipment_status']}` AS STRING), '')))
                NOT IN UNNEST(@excluded)"""
    sql = f"""
        SELECT DISTINCT
          UPPER(TRIM(CAST(`{columns['shipment']}` AS STRING))) AS shipment
        FROM `{MONITORING_TABLE}`
        WHERE `{columns['blocking_reason']}` IS NOT NULL
          AND TRIM(CAST(`{columns['blocking_reason']}` AS STRING)) != ''
          AND TRIM(CAST(`{columns['blocking_reason']}` AS STRING))
                NOT IN UNNEST(@excluded_reasons)
          AND NULLIF(TRIM(CAST(`{columns['shipment']}` AS STRING)), '') IS NOT NULL{status}
    """
    try:
        job = client.query(sql, job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ArrayQueryParameter("excluded_reasons", "STRING",
                                         list(EXCLUDED_REASONS)),
            bigquery.ArrayQueryParameter("excluded", "STRING",
                                         list(EXCLUDED_STATUSES)),
        ]))
        found = {str(row["shipment"]) for row in job.result()}
    except Exception as exc:  # noqa: BLE001 - better to resolve nothing than everything
        log(f"  The monitoring table's open shipments could not be read: {exc}")
        return None
    log(f"  {MONITORING_TABLE}: {len(found)} shipment(s) still reported as blocked.")
    return found


def fetch_ko_candidates(client: Any, log: Any, limit: int = 0) -> List[Dict[str, str]]:
    """
    Shipments the monitoring table calls blocked that the operational table
    does not hold yet.

    A row counts as blocked when it has a blocking_reason that is none of
    EXCLUDED_REASONS — those name things the documents API cannot fix — and
    when its shipmentStatus is neither BACKORDER nor CUSTOMER_CARE.

    Shipments already in the operational table are left out, because the
    merge would only update what the run already knows. Shipments in the
    history table are left out too: those were resolved and archived, and a
    monitoring row left over from before that would otherwise reopen them on
    every run. An upload that carries one again does reopen it — that is an
    explicit statement that it is KO, which a leftover row is not.
    """
    if bigquery is None or client is None:
        return []
    columns = _columns_of(client, PROJECT, DATASET,
                          "prd_fulfillment_monitoring_table", CANDIDATE_FIELDS, log)
    if "shipment" not in columns or "blocking_reason" not in columns:
        log(f"  {MONITORING_TABLE}: no shipment or blocking_reason column; "
            "no shipments are taken from it.")
        return []

    wanted = [name for name in CANDIDATE_FIELDS if name in columns
              and name not in ("shipment", "blocking_reason")]
    selects = "".join(
        f",\n          CAST(`{columns[name]}` AS STRING) AS {name}" for name in wanted
    )
    # BACKORDER and CUSTOMER_CARE are dropped at the source, so they are
    # neither taken in nor counted. If the table has no status column the
    # filter cannot be applied, and the log says so rather than passing them
    # through silently.
    status = ""
    if "shipment_status" in columns:
        status = f"""
          AND UPPER(TRIM(IFNULL(CAST(`{columns['shipment_status']}` AS STRING), '')))
                NOT IN UNNEST(@excluded)"""
    else:
        log(f"  {MONITORING_TABLE}: no shipmentStatus column; BACKORDER and "
            "CUSTOMER_CARE shipments cannot be excluded.")

    history = ""
    if _table_exists(client, HISTORY_TABLE, log):
        history = f"""
          AND UPPER(TRIM(CAST(`{columns['shipment']}` AS STRING))) NOT IN (
            SELECT UPPER(TRIM(shipment_order_number)) FROM `{HISTORY_FQN}`
            WHERE shipment_order_number IS NOT NULL
          )"""
    sql = f"""
        SELECT DISTINCT
          UPPER(TRIM(CAST(`{columns['shipment']}` AS STRING))) AS shipment_order_number,
          CAST(`{columns['blocking_reason']}` AS STRING) AS blocking_reason{selects}
        FROM `{MONITORING_TABLE}`
        WHERE `{columns['blocking_reason']}` IS NOT NULL
          AND TRIM(CAST(`{columns['blocking_reason']}` AS STRING)) != ''
          AND TRIM(CAST(`{columns['blocking_reason']}` AS STRING)) NOT IN UNNEST(@excluded_reasons)
          AND NULLIF(TRIM(CAST(`{columns['shipment']}` AS STRING)), '') IS NOT NULL
          AND UPPER(TRIM(CAST(`{columns['shipment']}` AS STRING))) NOT IN (
            SELECT UPPER(TRIM(shipment_order_number)) FROM `{FQN}`
            WHERE shipment_order_number IS NOT NULL
          ){status}{history}
        {f'LIMIT {int(limit)}' if limit and limit > 0 else ''}
    """
    try:
        job = client.query(sql, job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ArrayQueryParameter("excluded_reasons", "STRING",
                                         list(EXCLUDED_REASONS)),
            bigquery.ArrayQueryParameter("excluded", "STRING",
                                         list(EXCLUDED_STATUSES)),
        ]))
        rows = list(job.result())
    except Exception as exc:  # noqa: BLE001 - the run goes on without them
        log(f"  Could not read the monitoring table's KO shipments: {exc}")
        return []

    found: List[Dict[str, str]] = []
    for row in rows:
        entry = {"shipment_order_number": str(row["shipment_order_number"] or "").strip(),
                 "blocking_reason": str(row["blocking_reason"] or "").strip()}
        for name in wanted:
            entry[name] = str(row[name] or "").strip()
        if entry["shipment_order_number"]:
            found.append(entry)
    log(f"  {MONITORING_TABLE}: {len(found)} blocked shipment(s) not in the table yet.")
    return found


# ---------------------------------------------------------------------------
# The history table: where a resolved shipment goes
# ---------------------------------------------------------------------------

def _table_exists(client: Any, table: str, log: Any) -> bool:
    """Is this table in the dataset?"""
    if bigquery is None or client is None:
        return False
    try:
        job = client.query(
            f"SELECT COUNT(*) AS n FROM `{PROJECT}.{DATASET}`.INFORMATION_SCHEMA.TABLES "
            "WHERE table_name = @table",
            job_config=bigquery.QueryJobConfig(query_parameters=[
                bigquery.ScalarQueryParameter("table", "STRING", table),
            ]))
        for row in job.result():
            return int(row["n"]) > 0
    except Exception as exc:  # noqa: BLE001
        log(f"  Could not check whether {table} exists: {exc}")
    return False


def _live_columns(client: Any, log: Any, table: str = TABLE) -> List[str]:
    """
    A table's columns, in its own order, read from INFORMATION_SCHEMA.

    The archive works from what the tables actually hold rather than from the
    list this module declares, so a column added to one and not yet to the
    other does not silently stop being archived.
    """
    if bigquery is None or client is None:
        return []
    try:
        job = client.query(
            f"SELECT column_name FROM `{PROJECT}.{DATASET}`.INFORMATION_SCHEMA.COLUMNS "
            "WHERE table_name = @table ORDER BY ordinal_position",
            job_config=bigquery.QueryJobConfig(query_parameters=[
                bigquery.ScalarQueryParameter("table", "STRING", table),
            ]))
        return [str(row["column_name"]) for row in job.result()]
    except Exception as exc:  # noqa: BLE001
        log(f"  Could not read the columns of {table}: {exc}")
        return []


# The cancellation ledger, in this same dataset. The KO workflow never writes
# to it; it reads one question off it: which shipments has the warehouse been
# asked to cancel without confirming yet.
CANCELLATION_TABLE = "warehouse_orders_cancellation_table"

# The ledger resolves its own column names case-insensitively against the real
# schema, so the same candidates are honoured here rather than assuming the
# spelling this project happens to use.
CANCELLATION_FIELDS: Dict[str, Tuple[str, ...]] = {
    "shipment": ("shipment_order_number", "shipmentOrderNumber",
                 "shipment_number", "shipmentNumber"),
    "sent": ("cancellation_sent_to_warehouse", "cancellationSentToWarehouse"),
    "confirmed": ("cancellation_confirmed_by_warehouse",
                  "cancellationConfirmedByWarehouse"),
}


def fetch_awaiting_cancellation(client: Any, log: Any) -> Optional[Set[str]]:
    """
    The shipments GXO has been asked to cancel and has not confirmed.

    sent_to_warehouse true, confirmed_by_warehouse false. These are shipments
    the warehouse owes an answer on, so they carry the cancel action whatever
    the monitoring table says about them — a shipment can drop out of
    'Shipment cancelled on Kibo but still pending for GXO' while the request
    is still unanswered, and the action has to outlive the reason.

    Returns None rather than an empty set when the ledger cannot be read:
    empty means nobody is waiting, None means we do not know, and the caller
    must not take one for the other and clear actions it cannot account for.
    """
    if bigquery is None or client is None:
        return None
    columns = _columns_of(client, PROJECT, DATASET, CANCELLATION_TABLE,
                          CANCELLATION_FIELDS, log)
    missing = [name for name in CANCELLATION_FIELDS if name not in columns]
    if missing:
        log(f"  {CANCELLATION_TABLE}: no {', '.join(missing)} column; "
            "no cancel actions from the ledger.")
        return None

    # Both flags go through CAST(... AS STRING) so a column typed BOOL and one
    # typed STRING answer the same way; this ledger has been both. A null
    # confirmation counts as not confirmed, which is what an unanswered
    # request looks like before the reconciler has ever looked at it.
    sql = f"""
        SELECT DISTINCT
          UPPER(TRIM(CAST(`{columns['shipment']}` AS STRING))) AS shipment
        FROM `{PROJECT}.{DATASET}.{CANCELLATION_TABLE}`
        WHERE LOWER(IFNULL(CAST(`{columns['sent']}` AS STRING), '')) IN ('true', 'yes', '1')
          AND LOWER(IFNULL(CAST(`{columns['confirmed']}` AS STRING), 'false'))
              NOT IN ('true', 'yes', '1')
          AND `{columns['shipment']}` IS NOT NULL
    """
    try:
        rows = list(client.query(sql).result())
    except Exception as exc:  # noqa: BLE001 - one lookup, not the run
        log(f"  {CANCELLATION_TABLE}: could not be read: {exc}")
        return None
    found = {str(row["shipment"]) for row in rows if row["shipment"]}
    log(f"  {PROJECT}.{DATASET}.{CANCELLATION_TABLE}: {len(found)} shipment(s) "
        "sent to the warehouse and not confirmed yet.")
    return found


def ensure_history(client: Any, log: Any) -> str:
    """
    Create the history table if it is not there, and keep its schema in step.

    CREATE TABLE LIKE copies the operational table's schema exactly, so the
    archive never has to list columns to create. The two extra columns are
    added afterwards, and both statements are safe to run on every run.
    """
    if bigquery is None or client is None:
        return "no BigQuery client"
    sql = f"""
        CREATE TABLE IF NOT EXISTS `{HISTORY_FQN}`
        LIKE `{FQN}`
        OPTIONS (description = 'Shipments whose KO is resolved, moved out of {TABLE}. One row per shipment; a shipment that comes back and is resolved again updates its row here.');

        ALTER TABLE `{HISTORY_FQN}`
          ADD COLUMN IF NOT EXISTS archived_at TIMESTAMP
            OPTIONS (description = 'When this shipment was moved out of {TABLE}.'),
          ADD COLUMN IF NOT EXISTS archived_run STRING
            OPTIONS (description = 'The run that moved it.');
    """
    try:
        client.query(sql).result()
    except Exception as exc:  # noqa: BLE001
        log(f"  {HISTORY_FQN} could not be prepared: {exc}")
        return str(exc)
    return ""


def archive_resolved(client: Any, run: str, log: Any) -> Dict[str, Any]:
    """
    Move every resolved shipment into the history table.

    Two statements, in this order on purpose: the rows are copied first and
    deleted second, so a failure between the two leaves them in both places
    rather than in neither. The copy is a MERGE on the shipment number, which
    makes it safe to repeat: a shipment that was archived, came back and was
    resolved again updates its history row instead of adding a second one.
    """
    outcome: Dict[str, Any] = {"archived": 0, "deleted": 0, "error": ""}
    if bigquery is None or client is None:
        outcome["error"] = "no BigQuery client"
        return outcome
    error = ensure_history(client, log)
    if error:
        outcome["error"] = error
        return outcome

    # Only the columns BOTH tables have. The archive is created from the
    # operational table, but the two can drift — a column dropped from one and
    # not the other — and naming a column the archive has not got fails the
    # whole statement. An intersection archives everything it can and says
    # what it could not.
    columns = _live_columns(client, log, TABLE)
    archive_columns = set(_live_columns(client, log, HISTORY_TABLE))
    if not columns or not archive_columns:
        outcome["error"] = f"could not read the columns of {TABLE} or {HISTORY_TABLE}"
        return outcome
    missing = [column for column in columns if column not in archive_columns]
    if missing:
        log(f"  {HISTORY_TABLE} has not got {', '.join(missing)}; "
            "those columns are not archived. "
            f"Run sql/align_ko_history_table.sql to put that right.")
    columns = [column for column in columns if column in archive_columns]

    assignments = ",\n              ".join(f"{column} = S.{column}" for column in columns)
    names = ", ".join(columns)
    values = ", ".join(f"S.{column}" for column in columns)
    merge = f"""
        MERGE `{HISTORY_FQN}` AS T
        USING (
          SELECT * FROM `{FQN}`
          WHERE IFNULL(ko_resolved, FALSE)
            AND shipment_order_number IS NOT NULL
        ) AS S
        ON UPPER(TRIM(T.shipment_order_number)) = UPPER(TRIM(S.shipment_order_number))
        WHEN MATCHED THEN UPDATE SET
              {assignments},
              archived_at = @now,
              archived_run = @run
        WHEN NOT MATCHED THEN
          INSERT ({names}, archived_at, archived_run)
          VALUES ({values}, @now, @run)
    """
    parameters = [
        bigquery.ScalarQueryParameter("now", "TIMESTAMP", datetime.now(timezone.utc)),
        bigquery.ScalarQueryParameter("run", "STRING", run),
    ]
    try:
        job = client.query(merge, job_config=bigquery.QueryJobConfig(
            query_parameters=parameters))
        job.result()
        outcome["archived"] = job.num_dml_affected_rows or 0
    except Exception as exc:  # noqa: BLE001
        log(f"  Resolved shipments could not be archived: {exc}")
        outcome["error"] = str(exc)
        return outcome

    if not outcome["archived"]:
        return outcome

    try:
        job = client.query(
            f"DELETE FROM `{FQN}` WHERE IFNULL(ko_resolved, FALSE) "
            "AND shipment_order_number IS NOT NULL")
        job.result()
        outcome["deleted"] = job.num_dml_affected_rows or 0
    except Exception as exc:  # noqa: BLE001 - archived but not removed: next run retries
        log(f"  Archived, but {FQN} could not be cleared: {exc}")
        outcome["error"] = str(exc)
        return outcome

    log(f"  {outcome['archived']} resolved shipment(s) archived to {HISTORY_TABLE} "
        f"and removed from {TABLE}.")
    return outcome
