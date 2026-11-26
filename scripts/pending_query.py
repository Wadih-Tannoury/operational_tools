"""
The pending-monitoring list, queried instead of uploaded.

What somebody used to export from the console and upload as
`…__Wadih__pending.csv` is a query against

    tlg-business-intelligence-prd.til.b2c_pending_shipments_analysis

and a handful of tables beside it. The query lives in
`sql/pending_shipments.sql`, next to the rest of the SQL, so it can be read
and run by hand without going through Python.

Its rows are handed to build_rows under the same keys read_csv produces, so
everything downstream — the join, the documents call, the OERODP — cannot
tell the difference between a queried row and an uploaded one. That is the
point: the upload still works, and it still wins when it is there.

Nothing is written to disk. The query is the record of what a run saw, and a
CSV of it in the repository would only be a second copy going stale.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

# The query's own aliases on the left, the keys read_csv produces on the
# right. Anything the query selects and this does not name is ignored, so the
# query can grow columns for other readers without disturbing this one.
QUERY_ALIASES = {
    "ship": "shipment",
    "logord": "logistic_order",
    "oms": "oms_shipment",
    "submit": "order_date",
    "created": "shipment_created",
    "createdEff": "shipment_created_effective",
    "action": "action",
    "task": "active_task",
    "id": "external_order",
    "brand": "brand",
    "ch": "channel",
    "days": "pending_days",
    "ev": "wh_last_event",
    "lblerr": "ship_label_error",
    "rlblerr": "return_label_error",
    "method": "shipping_method",
    "cc": "country",
    "peso": "weight",
    "colli": "parcels",
    "koReason": "gxo_ko_reason",
    "lbl": "label_succeeded",
    "trk": "csv_tracking_number",
    "rtrk": "csv_return_tracking",
    "carrier": "carrier",
    "stus": "status",
    "wms": "wms",
    "flc": "location_name",
}

SQL_FILE = Path(__file__).resolve().parent.parent / "sql" / "pending_shipments.sql"


def sql_text() -> str:
    """The query, read from the file it lives in."""
    return SQL_FILE.read_text(encoding="utf-8")


def fetch(client: Any, log: Any, limit: int = 0) -> List[Dict[str, str]]:
    """
    The pending list, in read_csv's shape. [] when it cannot be read.

    A failure here is not fatal. The run still has GXO's own KO list and
    whatever the operational table already holds, and saying so plainly beats
    stopping a run that could still do most of its job.
    """
    if client is None:
        log("  No BigQuery client, so the pending list cannot be queried.")
        return []
    try:
        sql = sql_text()
    except OSError as exc:
        log(f"  {SQL_FILE.name} could not be read: {exc}")
        return []
    if limit > 0:
        sql = f"SELECT * FROM (\n{sql}\n) LIMIT {int(limit)}"

    try:
        result = client.query(sql).result()
    except Exception as exc:  # noqa: BLE001 - the run goes on without it
        log(f"  The pending-shipments query failed: {exc}")
        return []

    rows: List[Dict[str, str]] = []
    for record in result:
        row: Dict[str, str] = {}
        for alias, key in QUERY_ALIASES.items():
            try:
                value = record[alias]
            except (KeyError, IndexError):
                continue
            if value is None:
                continue
            text = str(value).strip()
            if text:
                row.setdefault(key, text)
        if row.get("shipment"):
            rows.append(row)
    log(f"  The pending-shipments query returned {len(rows)} shipment(s).")
    return rows
