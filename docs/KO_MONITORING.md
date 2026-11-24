# 4. KO monitoring

The third tool in this repository, and the first that produces a dashboard
rather than a change. Nothing is cancelled, sent or written to any system: it
reads, decides, and commits a dataset.

| | |
|---|---|
| Queue | `ko_monitoring/input/` `output/` `archive/` `error/` `logs/` |
| Workflow | `.github/workflows/ko_monitoring.yml` |
| Worker | `scripts/build_ko_monitoring.py`, `scripts/ko_operational_table.py` |
| Table | `tlg-business-intelligence-prd.til.ko_operational_table` — `sql/ko_operational_table.sql` creates it, `sql/migrate_ko_operational_table.sql` rebuilds an older one |
| Also writes | `til.ko_operational_history_table` (resolved shipments) |
| Also read | `til.prd_fulfillment_monitoring_table` (ko_reason, location_provider), `bi.wh_document_api_response` (the warehouse's attempts), `tlg-wlfs-prd.monitoring.gxo_ko` (the warehouse's own KO list), `tlg-wlfs-prd.til.prd_shipmentStatus` (SHIPPED events) |
| Credentials | `BI_BIGQUERY_CREDS` for everything in `tlg-business-intelligence-prd`, `WLFS_BIGQUERY_CREDS` for everything in `tlg-wlfs-prd` — `monitoring.gxo_ko` and `til.prd_shipmentStatus`. Neither account is ever used against the other's project. |
| Secrets | `GET_DOCUMENTS_API_CREDENTIALS`, `MODA_GET_DOCUMENTS_API_CREDENTIALS`, `BI_BIGQUERY_CREDS`, `WLFS_BIGQUERY_CREDS` |
| Console | the **ko-monitoring-app** repository, deployed on Streamlit |

**Run `sql/ko_operational_table.sql` once** in the BI project before the first
run. It is `CREATE TABLE IF NOT EXISTS` followed by `ADD COLUMN IF NOT EXISTS`,
so it is safe to run again and leaves every existing column alone.

The queue is its own, so this workflow runs beside the two cancellation ones
and shares no folder with them.

## What a batch is

The console commits **two files in one commit**: the pending-monitoring CSV and
the IMKO check workbook, both carrying the same batch stamp, which is
everything before the first `__` in the name.

```
ko_monitoring/input/20261120-104501__Wadih__pending.csv
ko_monitoring/input/20261120-104501__Wadih__check.xlsx
```

One commit is one push event, so the run starts once with the pair already
complete. A batch missing its other half is left in `input/` and named in the
log rather than processed; the next run picks it up once the pair arrives.

## What the worker does

**The CSV**: every row, nothing filtered. The headings it reads are matched
ignoring case, spaces, underscores, dots, `#`, brackets and slashes, so
`OMS shipment #` and `oms_shipment_number` are one column, and so are
`Shipment created (effective)` and `shipment_created_effective`.

Either export works. The **detail** export (80 columns) is the one to upload:
it carries `Logistic order`, `OMS shipment #` and `Shipment created
(effective)`, which the shorter 23-column export does not, and also `Peso` and
`Colli`, which become the `weight` and `parcelsNumber` of the documents call
instead of the script's 1.0 and 1 — and `is_ko` and `GXO KO reason`, which go
on the dashboard beside the action they explain. Upload the short export and
all of those stay empty; nothing else changes, and `Logistic order` is then
filled in from the warehouse's own attempt row where there is one.

**The workbook**: the `imko` sheet only, and only the rows whose `ub_imko` is
one of `IMKO 001`–`IMKO 026` or `IMKO 100`–`IMKO 199`. The sheet spells these
with a varying run of spaces — `IMKO  001`, `IMKO   001` — which is normalised
away before the comparison, so a code is matched on its number, not its
spacing. Every other code, `IMKO 027` and `IMKO 028` among them, is dropped.

**The logistics order number.** It goes to the documents endpoint without its
two-character brand prefix — `DJ141651` is sent as `141651`, the same number
the detail export carries as `OMS shipment #` — while the dashboard and the
table keep the full value.

Where it comes from: The CSV's `Logistic order` when the export
carries it; otherwise what OERODP holds after its first hyphen —
`11E-DJ141651` is logistics order `DJ141651`. That is the only source an
excel-only shipment has, and it is what gives those rows a number for the
documents call and for the warehouse lookup. The rule was checked against the
detail export on every shipment the two files share: 86 of 86 agreed, and on
the sample every one of the 421 rows ends up with a number. A shipment whose
OERODP values derive to more than one logistics order is left empty rather
than guessed at, because a wrong number would cost the row its warehouse
history. Failing both, the number is read back from the warehouse's own
attempt row.

**The join**: `VCVAIC` on the workbook = `Shipment` on the CSV. The workbook
holds one row per product, so its side is collapsed first — a shipment with
several `OERODP` or `ub_imko` values keeps all of them, distinct and joined by
`; `, rather than one being picked silently. **The dataset is one row per
shipment.**

**A shipment in the workbook but not in the CSV is kept**, with every CSV
column left empty — that is what `--join outer`, the default, means. On the
sample files that is 130 shipments of the 421: 205 from the CSV alone, 86 on
both sides, 130 from the workbook alone.

| `--join` | |
|---|---|
| `outer` (default) | every CSV shipment, **plus the ones the filtered workbook adds** |
| `csv` | the CSV only |
| `inner` | only shipments both files carry |

The `source` column on every row says which side it came from — `csv + excel`,
`csv only` or `excel only` — so a shipment only one of the two files knows
about is visible rather than quietly dropped.

## OERODP for a shipment the workbook does not carry

Built as **brandCode + the channel's letter + `-` + the logistics order
number**: `DJ-EUA00366315` on `ecommerce` with logistics order `DJ141651`
gives `11E-DJ141651`. The brand is the first two characters of the shipment
number and `BRAND_CODES` in the worker maps it to its code; the channel gives
its first letter, upper-cased — `ecommerce` E, `Farfetch` F, `Miinto` M.

`CHANNEL_LETTERS` overrides that for a channel that does not follow it;
`GLAFAYETTE` is there with an empty letter, because the workbook spells that
shipment `11-DJ152574` rather than `11G-DJ152574`. With that one exception the
rule reproduces the workbook's own value on 86 of the 86 shipments the two
files share. An unknown brand or a missing logistics order leaves OERODP empty
rather than half-built.

## The monitoring table, and the shipments it reports

`til.prd_fulfillment_monitoring_table` is **read, not written**: whatever keeps
it up to date does so on its own schedule, and this workflow takes it as it
stands. It is read on every run, upload or not, and so is
`monitoring.gxo_ko` — a shipment either of them has just started reporting is
a KO whether or not anyone uploaded anything today. On a run with an upload,
gxo_ko is already the GXO side of the join; on a run without one it only ever
adds shipments the tables do not hold.

Every shipment it reports with a `blocking_reason` is
taken into the operational table, upload or no upload. Two kinds of row are
left out:

- **`blocking_reason` is null** — nothing is wrong with the shipment.
- **`blocking_reason` is one the documents API cannot fix** — `KO fixed,
  already shipped` (the shipment left the warehouse), `Shipment packed at GXO
  but not shipped and still READY in OMS` (the warehouse has done its part;
  what is stuck is the OMS status) and `GXO shared the wrong quantity` (a stock
  disagreement, settled between the warehouse and the inventory app). A
  shipment carrying one would sit in the table being re-asked for ever.
- **`shipmentStatus` is `BACKORDER` or `CUSTOMER_CARE`** — those are diagnosed
  on the stock picture the order was taken against, not on the warehouse's
  documents, so there is nothing here to re-try; they would sit in the table
  being asked about for ever.

A shipment the operational table already holds is left alone, so this only
ever adds; one already in the history table is left alone too, so a monitoring
row left over from before it was resolved cannot reopen it on every run. An
upload that carries it again does reopen it — that is a statement that it is
KO, which a leftover row is not.

These shipments carry `source = MONITORING` and then go through the same
checks as every other: the documents API, the warehouse's attempts, and the
monitoring lookup that fills in `ko_reason` and `location_provider`.

## What GXO is asked to do

`action_needed_from_gxo` is filled for GXO shipments only — it is the
warehouse's own worklist, and an action against a MODA shipment would be read
as something to chase GXO about:

| when | action |
|---|---|
| `blocking_reason` is `Shipment cancelled on Kibo but still pending for GXO` | `To cancel the shipment` |
| `ko_fixed` is true | `To print shipping documents` |
| otherwise | empty |

The cancellation comes first: a shipment Kibo has cancelled is to be cancelled
whether or not its documents would now print.

`ko_source` and `blocking_reason` are taken from
`til.prd_fulfillment_monitoring_table` as they stand there, and overwritten on
every run — they describe the shipment now, not when it was first seen.

A shipment whose `blocking_reason` starts with `Stock still reserved to the
cancelled shipment` is kept off both console dashboards: nothing can be asked
of the warehouse until the shipment named in that reason is dealt with. The
row stays in the table.

## Two columns the dashboards need

`channel` comes from the CSV, and from the monitoring table for a shipment no
CSV carried.

`in_latest_gxo_input` says whether the shipment was in the latest IMKO
workbook or the latest `monitoring.gxo_ko` import. It is set by one statement
over the whole table after every run that had **both** inputs: the shipments
in them get TRUE and every other row FALSE in the same pass. A run with no
upload leaves the column exactly as it was, so "the latest upload" keeps
meaning the latest upload rather than the latest run.

Together with `last_tried_by_gxo_date` they are what the console's GXO view is
built from: tried at least once, **or** in the latest inputs.

## What is filled in before anything is called

`location_provider` picks the endpoint and `fulfillment_status` decides
whether the documents are worth asking for, so both are filled from
`til.prd_fulfillment_monitoring_table` **before** the first call rather than
after it. They used to be filled by the monitoring lookup, which runs at the
end: a shipment whose row was blank was recorded as *not asked: no
fulfillment_status* and then, a few lines later, given the very status that
would have had it asked. It waited a whole run for an answer the table already
had.

The monitoring table is read once per run and remembered, so the prefill and
the lookup at the end share one query.

## Asking Kibo what a shipment is waiting on

The table's `fulfillment_status` is often blank — a shipment taken from
`gxo_ko` has none at all — and the documents API is only worth calling for a
shipment in **Prepare for Shipment**. So before any call is made, every GXO
shipment whose stored status is not that one is looked up in Kibo with
`getShipments`:

Two things bring a shipment into this lookup, and either is enough:

- **a GXO shipment whose fulfilment status is not `Prepare for Shipment`** —
  the answer decides whether its documents are worth asking for;
- **any shipment with no `channel`** — Kibo is the only place it is written.
  The monitoring table carries it only for the rows it brought in, which is
  why a shipment from an upload or from `gxo_ko` had none.

The channel is a property of the **items**, not of the shipment, and which
list holds them depends on what has happened to it: `items` while it is live,
`canceledItems` or `reassignedItems` once it has been cancelled or moved. All
of those are read in turn, and the shipment's own `data.channel` is the last
resort.

| Kibo says | what happens |
|---|---|
| `Prepare for Shipment` | the status is written to the row and the documents API is asked **in this same run** |
| `shipmentStatus = FULFILLED` | it has left: marked fixed, and archived by the resolve pass |
| any other status | written to the row and logged; nothing is asked of the warehouse |
| nothing | logged |

The last two are asked again on **every** run, which is the point: a shipment
in "Validate Items In Stock" today may be ready tomorrow, and nothing else
would ever notice.

The fulfilment status is not a field of its own on a Kibo shipment — it is the
name of the workflow task that is currently active, so it is read from
`workflowState.activeTaskName`, and from the task list when that is empty.

This needs `KIBO_API_CREDS`, and the brand-to-tenant map that the cancellation
workflows use; the client, the tenant resolver and the site map are imported
from those scripts rather than written again. Without the secret the lookup is
skipped and the log says why — including the import error, if that is what went
wrong. `no_kibo` turns the stage off.

## The history table, and when a shipment goes into it

`til.ko_operational_history_table` is where a resolved shipment goes. At the
end of every run the resolved rows are copied there and removed from
`til.ko_operational_table`, so the operational table holds open shipments only.

Archiving is not undone by a later run, so the rules are narrow — a shipment
is archived only when **its own source has stopped reporting it**:

| | archived when |
|---|---|
| **GXO, asked to cancel** | `til.prd_shipmentStatus` has a `CANCELLED` event for the shipment number **and** the logistics order number together |
| **GXO, everything else** | `til.prd_shipmentStatus` has a `SHIPPED` event for the pair, **or** the run carried a workbook and the shipment is in neither that workbook nor the latest `monitoring.gxo_ko` import |
| **everything else** | the shipment is no longer in the filtered view of `til.prd_fulfillment_monitoring_table` — the same filters the intake applies |

A `SHIPPED` event settles a GXO shipment whatever any listing says: it left
the warehouse. Both events are matched on the shipment number **and** the
logistics order number, because a shipmentOrderNumber can be reused and an
event belonging to an earlier instance must not settle the one that is open
now; a feed carrying no logistics order number at all falls back to the
shipment number, and the log says so.

If `prd_shipmentStatus` cannot be read, no GXO shipment is archived on an
event that run — not knowing is not the same as nothing having happened.

A shipment carrying `action_needed_from_gxo = To cancel the shipment` answers
to the cancellation event and to nothing else: not the workbook, not gxo_ko.
We asked the warehouse to cancel it, and the event is the warehouse saying it
did — dropping off a listing is not an answer to that. It is also the one case
where a GXO shipment can be archived on a run with no workbook. The event has
to match **both** numbers, so a `CANCELLED` belonging to a different logistics
order on the same shipment number does not count.

Two things follow from the GXO rule, both deliberate:

- A GXO shipment in the last workbook or the latest gxo_ko is **never**
  archived, whatever else is true of it.
- **A run with no workbook archives no GXO shipment at all.** Absence from
  something that was not read is not absence. The log says how many were left
  alone.

The same caution applies on the other side: if the monitoring table cannot be
read, no non-GXO shipment is archived that run. An empty answer and a failed
read look alike, and treating a failed read as "nothing is blocked any more"
would archive the whole table at once.

A blank `location_provider` counts as GXO when `source` says GXO. Erring that
way is the safe one — a shipment wrongly called GXO is archived less often,
not more.

The copy is a MERGE on `shipment_order_number` and comes **before** the
delete, so a run that fails between the two leaves the rows in both places
rather than in neither; the next run repeats the MERGE and finishes the job.

`sql/restore_ko_archived.sql` brings archived shipments back: it copies them
into the operational table with `ko_resolved` cleared and takes them out of
the archive. Narrow its step 0 to choose which.

## A run with no upload

**Every run checks, upload or no upload.** With an empty queue the worker reads
the table for every shipment with `ko_fixed = false` and `ko_resolved = false`
and asks the documents API about each one, exactly as it would for a freshly
joined upload. The warehouse fixes a KO at its own pace, and the only way to
find out is to ask — waiting for someone to upload the same files again would
mean a shipment stays KO on the dashboard long after it stopped being KO.

**Such a run resolves nothing.** `ko_resolved` means a shipment has dropped
out of the uploaded files, and a run with no files has nothing to compare
against. Only a run that processed a complete batch, and processed all of it,
marks anything resolved.

The run is then recorded like any other: the rows merge into the table, the
dataset is written to `output/`, and the log says `recheck-<stamp>` as the
batch. With `--no-table`, or no BigQuery credentials, there is no table to
re-check and the run reports nothing to do, as before.

## ko_fixed, and what a later run re-checks

`ko_fixed` is `TRUE` whenever `ko_fix_date` is set, and it is derived from the
date on every write, so the two can never disagree.

It is also what decides the work of the next run: **only the shipments where
`ko_fixed` is FALSE are asked again.** A shipment the table already records as
fixed keeps its stored `ko_fix_date` and its evidence — the labels, the
tracking numbers, the API message — and costs no request. Its CSV columns,
OERODP and warehouse attempt are still refreshed from the new upload, because
those are current.

`--recheck-all` asks every shipment again regardless.

## The GXO side: the workbook and gxo_ko

On a run with new files the worker also reads the warehouse's own KO list:

```sql
SELECT * FROM `tlg-wlfs-prd`.monitoring.gxo_ko
WHERE importDate = (SELECT MAX(importDate) FROM `tlg-wlfs-prd`.monitoring.gxo_ko)
```

A shipment there is GXO's whether or not either file mentions it, so the GXO
side of the join is **the workbook plus gxo_ko**. Those shipments carry `GXO`
in `source` — `GXO only` when no CSV row matches, `TLG + GXO` when one does —
and `location_provider` GXO. Without `WLFS_BIGQUERY_CREDS` the run falls back
to the two files and says so.

## ko_resolved

A shipment is resolved when nothing further is going to happen to it here. The
rule depends on whose warehouse it is in:

| `location_provider` | resolved when |
|---|---|
| `GXO` | it is in neither the last workbook nor the last gxo_ko — the warehouse has stopped listing it — **or** `prd_shipmentStatus` has a SHIPPED event for its shipment **and** logistics order together |
| `MODA` | `ko_fixed` is true — MODA's own endpoint answers for the shipment directly, so a label it can print is the end of the matter |
| anything else | `ko_fixed` is true; there is no warehouse feed to ask |

This runs as its own pass over every unresolved row, not only the ones in the
upload, so a shipment resolved by a SHIPPED event is caught even on a run that
never mentions it. **A run that saw no workbook and no gxo_ko resolves nothing
for absence** — absence from nothing means nothing — though SHIPPED and
ko_fixed still apply.

Nothing is ever deleted: a resolved row keeps everything it had, and
`ko_resolved_date` says when it left the working list.

## ko_reason

Why the shipment is stuck, taken from the first source with anything to say:

| | |
|---|---|
| 1 | `external_error_message` from `til.prd_fulfillment_monitoring_table` |
| 2 | `error_message` from the same table |
| 3 | `blocking_reason` from the same table |
| 4 | the documents API's own error, via `bi.wh_document_api_response` |
| 5 | `action`, from the CSV — the least specific, so last |

The monitoring table is matched on the shipment number, and its column names
are read from `INFORMATION_SCHEMA` rather than assumed: a column renamed there
costs this run that one field, not the whole lookup.

## location_provider

Whose warehouse the shipment is in. **GXO** when the workbook carries the
shipment at all — it is GXO's own check file — or when the CSV's `WMS` column
says so; **MODA** when `WMS` says that. On the sample files that answers 353 of
421 rows. For the rest, `location_provider` from the monitoring table is used.

## ko_fix_date

Which endpoint a shipment is asked about depends on its warehouse, and two
warehouses are the whole list:

| `location_provider` | endpoint | credentials |
|---|---|---|
| `GXO` | `POST /logistics/v1/shipments/documents` | `GET_DOCUMENTS_API_CREDENTIALS` — client credentials, token call first |
| `MODA` | `POST /warehouse/v1/moda/getlabel` | `MODA_GET_DOCUMENTS_API_CREDENTIALS` — a bearer token, no token call first |
| anything else | **nothing is called** | — |

A shipment at FIEGE, GEODIS, CAPACITY or any other warehouse is **recorded and
left alone**: it keeps its row, keeps `ko_fixed = false`, and `ko_status` says
why. There is nobody to ask, and a call that cannot succeed still costs a
round trip and still writes a verdict into the table as though it meant
something.

A blank `location_provider` falls back to `source`: a shipment that came from
the GXO side of an upload is GXO's whether or not the column has caught up.
A blank provider with no GXO in its source is not called.

A missing MODA secret costs the MODA shipments their check, not the run: they
are left unfixed with `ko_status` saying so, and the rest carries on.

### GXO and the rest

The worker calls the shipment-documents API once per shipment — the same two
calls the desktop script makes, a client-credentials token and then
`POST /logistics/v1/shipments/documents` — and reads the response:

`ko_fix_date` is the **timestamp of that call** when all of these hold:

- `eligibleForPackageClosure` is `true`, and
- the `SHPLBL` label is populated **and in a printable format**, and
- the shipping tracking number is populated, and
- **either** the `RETLBL` label is populated and printable and the return
  tracking number is populated,
- **or** no `RETLBL` entry exists in the response at all.

It is left **empty** when a `RETLBL` entry exists but carries no label data:
the return label was expected and is missing, so the shipment is still KO. That
is the one case the three rules turn on, and the `ko_status` column spells out
which rule a row fell under.

### MODA

MODA returns one label for the order rather than the SHPLBL/RETLBL pair, and
has no package-closure flag, so there is less to weigh: `ko_fix_date` is the
timestamp of the call when the response carries **a label the warehouse can
print**, under the same `--label-check` rule. The label is recorded in the
`label_shplbl_*` columns and the return ones stay empty.

The request is the shipment number and a parcel of one:

```json
{ "orderNumber": "DJ-USAST0000124549", "weight": 1, "packagesNumber": 1 }
```

The response body is **searched rather than indexed**: the label is whatever
long string sits under a label-ish key (`labelData`, `content`, `zpl`,
`base64` and the like), at any depth, and its format is sniffed the same way.
That is deliberately forgiving because the shape of this response has not been
pinned down; once a real one is in the log it can be narrowed to the one path
it uses.

#### The MODA backfill

`moda_backfill` asks MODA's endpoint about the MODA shipments **already in the
table**, which the ordinary `backfill` never does — that one calls no API at
all. It is for the stretch where MODA shipments went unchecked or to the wrong
endpoint, so their `ko_fixed` cannot be trusted.

| input | what it asks about |
|---|---|
| `moda_backfill` | MODA shipments with `ko_fixed = false` and `ko_resolved = false` |
| `moda_backfill` + `moda_backfill_all` | every MODA shipment, fixed ones included |

With `moda_backfill_all` the fixed flag is cleared before the call, since
re-asking a shipment already marked fixed would otherwise be skipped.
`ko_fix_date` is never overwritten — the merge keeps the first one. The run
does nothing else: no upload is processed, no gxo_ko or monitoring intake
happens, and `--limit` caps it.

### Which label formats count

`--label-check printable`, the default: a label counts when it is **ZPL or
PDF**. Both are printable at the warehouse — ZPL goes to the label printer and
a PDF is printed as a page — so either one means the shipment can go.

The format is detected rather than assumed. The response's own field is
believed first (`labelFormat`, `format`, `documentFormat`, …); with no such
field the label data itself settles it, raw or base64-decoded: a ZPL stream
opens with `^XA` and is full of `^`-prefixed commands, a PDF opens with
`%PDF`, a PNG with its own signature. The three never collide.

What it found is written to `label_SHPLBL_format` and `label_RETLBL_format` —
`ZPL`, `PDF`, `PNG` or `other` — so the dashboard says which format each label
came in rather than a bare yes or no, and `ko_status` names it too: *fixed:
shipping label PDF, no return label expected*.

The other two modes:

| `--label-check` | |
|---|---|
| `printable` (default) | ZPL or PDF |
| `zpl` | ZPL only, the original stricter reading |
| `any` | any populated label, whatever the format |

A label in a format none of these accept is reported as *the shipping label is
PNG, which the warehouse cannot print*, and the shipment stays KO.

## last_tried_by_gxo_date and get_documents_error

From

```
`tlg-business-intelligence-prd`.bi.wh_document_api_response
```

matching `shipment_order_number` against the shipment, and
`logistics_order_number` against the row's `Logistic order` **when the CSV
supplies one**. A CSV without that column still matches on the shipment number
alone, so a missing column costs precision on the join and never a row.

| What is found | `last_tried_by_gxo_date` | `get_documents_error` |
|---|---|---|
| A row with `result_code = Succeeded` | its `created_on`, the newest one | empty |
| No `Succeeded` row, but other rows | the newest `created_on` of any result code | its `error_message`, or its `result_code` when that is empty |
| Nothing at all | empty | empty |

`--bq-lookback-days N` limits the read to the last N days when the table grows
large enough for that to matter; `0`, the default, reads all of it.

Without `BI_BIGQUERY_CREDS` the run still builds the dashboard and says once in
the log that these two columns stay empty. The dataset is never lost to a
BigQuery problem.

## The KO operational table

Everything above is one run. The table is every run:

```
`tlg-business-intelligence-prd`.til.ko_operational_table
```

One row per shipment, keyed on `shipment_order_number` (trimmed and
upper-cased on both sides, so a stray space is still the same row). At the end
of each run, `scripts/ko_operational_table.py`:

1. **Merges** the run's shipments in. A shipment already in the table has its
   row **updated — never duplicated** — and `times_seen` goes up by one. A new
   one is inserted with `row_insertion_date` and `times_seen = 1`.
2. **Keeps `ko_fix_date`.** Once a run has found a shipment fixed, that
   timestamp stays, even if a later run of the same shipment comes back KO.
   The merge sets `COALESCE(T.ko_fix_date, S.ko_fix_date)` and nothing else
   touches it.
3. **Resolves what has dropped out.** Every row still open whose shipment is
   **not** in the new upload gets `ko_resolved = TRUE` and
   `ko_resolved_date`. That is how a shipment leaves the dashboard without
   being deleted: the warehouse has dealt with it, and the row stays as the
   record that it once was KO.
4. **Reopens what comes back.** A shipment that appears in a later upload has
   `ko_resolved` set back to `FALSE` and the date cleared.

`ko_resolved` is `FALSE` on every row the moment it is inserted, which is what
"false by default" means here; the column is also `NULL`-safe everywhere —
`IFNULL(ko_resolved, FALSE)` — so rows written before the column existed read
as open rather than as resolved.

### When nothing is resolved

A run that did not look at the whole upload resolves nothing, and says so in
its log. That is any run with `--limit`, and every dry run. The reasoning: a
shipment that was never checked has not been resolved, it has merely not been
looked at, and marking it resolved would quietly empty the dashboard.

### How it is written

One staging table, one `MERGE`, one `UPDATE`, and the staging table is dropped
again — four hundred shipments would otherwise be four hundred queries. The
staging table is `til._ko_monitoring_staging_<run>` and lives for the length of
the run.

`--no-table` skips all of it. So does a missing `BI_BIGQUERY_CREDS`: the run
says so once and writes its CSV as usual. **A table that cannot be reached
costs the run its history, never its dataset.**

### What it costs in permissions

The `BI_BIGQUERY_CREDS` service account needs `roles/bigquery.dataEditor` on
the `til` dataset — the same role it already has for
`til.warehouse_orders_cancellation_table`. The console's own service account
needs read only.

## What it writes

```
ko_monitoring/output/ko_monitoring-<stamp>.csv   the run's dataset, kept
ko_monitoring/output/latest.csv                  what the dashboard reads
ko_monitoring/output/latest.json                 run metadata and per-batch counts
ko_monitoring/logs/run-<stamp>.log               the log
ko_monitoring/logs/run-<stamp>.csv               the dataset again, beside the log
```

and merges the same rows into `til.ko_operational_table`, which is what the
dashboard actually reads. The files under `output/` are the backup and the
fallback: the console uses them when it has no BigQuery credentials.

and moves the two input files to `ko_monitoring/archive/`, or to
`ko_monitoring/error/` if the batch could not be read at all. A batch that
fails does not stop the others and does not fail the run: the log says what
went wrong and the files land in `error/`.

The dataset's columns are the ones the dashboard shows first —
`shipmentOrderNumber`, `OERODP`, `ub_imko`, `Logistic order`, `OMS shipment #`,
`Order date`, `Shipment created`, `Shipment created (effective)`, `Action`,
`Active task`, `ko_fix_date`, `last_tried_by_gxo_date`, `get_documents_error` —
followed by the evidence behind each `ko_fix_date`: `ko_status`, `source`, the
label and tracking flags, the API's message and HTTP status, and the batch.

## Secrets

| Secret | Value |
|---|---|
| `GET_DOCUMENTS_API_CREDENTIALS` | `{"clientId": "...", "clientSecret": "..."}` |
| `MODA_GET_DOCUMENTS_API_CREDENTIALS` | `{"BEARER_TOKEN": "..."}` |
| `BI_BIGQUERY_CREDS` | the service-account JSON for `tlg-business-intelligence-prd` — **already set** for the other workers; this one needs read on `bi.wh_document_api_response` |

Optional repository variables `GET_DOCUMENTS_AUTH_URL` and `GET_DOCUMENTS_URL`
point a run at a test endpoint.

## Running the workflow by hand

`Actions → KO monitoring → Run workflow`. The inputs worth knowing:

| Input | What it does |
|---|---|
| `dry_run` | joins the two files and reports, calls nothing. The first thing to run on a new pair of files |
| `join` | `outer`, `csv` or `inner`, as above |
| `label_check` | `printable`, `zpl` or `any` |
| `backfill` | go over **every** shipment in the table, resolved ones included, and fill in what can be filled in. Calls no documents API, resolves nothing, changes no verdict |
| `recheck_all` | ask the documents API again even for shipments already marked `ko_fixed` |
| `no_bigquery` | skip the warehouse-attempt lookup |
| `no_table` | do not write `til.ko_operational_table` |
| `limit` | check at most N shipments — a quick check on a handful before a full run |
| `sleep` | seconds between API calls, `0.1` by default |

A manual run works on whatever is already in `input/`. After a successful run
that is nothing — and that is now the useful case: the run re-checks every
shipment the table still has open. Uploading the pair again is only needed when
the set of KO shipments itself has changed, because that is what decides
`ko_resolved`.

Worth adding a schedule if these should happen on their own:

```yaml
on:
  schedule:
    - cron: "17 7 * * 1-5"   # weekday mornings, UTC
```

## Local run

```bash
pip install -r requirements.txt
export GET_DOCUMENTS_API_CREDENTIALS='{"clientId":"...","clientSecret":"..."}'
export MODA_GET_DOCUMENTS_API_CREDENTIALS='{"BEARER_TOKEN":"..."}'
export BI_BIGQUERY_CREDS="$(cat service-account.json)"
cp pending.csv  ko_monitoring/input/20261120-120000__local__pending.csv
cp check.xlsx   ko_monitoring/input/20261120-120000__local__check.xlsx
python scripts/build_ko_monitoring.py --dry-run --keep-input
```

`--keep-input` leaves the files where they are, so the same pair can be run
again while a flag is being tuned.

## Things worth knowing

**It is read-only everywhere but this repository.** The documents API call is
what the warehouse itself would make; nothing is booked, cancelled or printed.
BigQuery is only read. The only writes are the files under `ko_monitoring/`.

**One call per shipment.** A few hundred shipments is a few minutes, and
`--sleep` is what keeps it polite. `--limit` is there for a first look.

**A failed call is a row, not a crash.** The shipment keeps its line with the
HTTP status and the message in `api_http_status` and `api_message`, and no
`ko_fix_date`.

**Repository growth.** Every run keeps its dataset under `output/` as well as
overwriting `latest.csv`. At a run or two a day this is small; if it ever
matters, delete `ko_monitoring/output/ko_monitoring-*.csv` older than a few
months — `latest.csv` is what the dashboard reads.

## What the dashboard shows

The console shows **open GXO shipments only**: `ko_resolved = false` and `GXO`
in `source`. That is the working list — the shipments someone can still do
something about at the warehouse this console talks to. Everything else stays
in the table and is simply not on the page; the *Hidden* metric counts it, and
the evidence columns carry `ko_resolved` for anyone who wants to see which.
