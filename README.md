# Operational tools

The workers. Each tool is a script in `scripts/`, a workflow in
`.github/workflows/`, and a queue of four folders it owns. A CSV lands in that
tool's `input/`, the workflow applies it, writes a log and moves the file to
`archive/` or `error/`. No server to run, no machine that has to stay on,
nothing to pay for.

The consoles that queue the files live in the separate **applications**
repository, one page per tool. Nothing in this repository serves a web page.

```
  applications (Streamlit)        this repo                    GitHub Actions
 ┌──────────────────────┐   ┌──────────────────────┐   ┌──────────────────────────┐
 │ Kibo page            │──▶│ input/*.csv          │──▶│ process_cancellations    │
 │                      │   │ archive/ error/ logs/│◀──│  BigQuery + Kibo         │
 │ Warehouse page       │──▶│ warehouse/input/*.csv│──▶│ process_warehouse_       │
 │                      │   │ warehouse/archive/ … │◀──│  cancellations           │
 │ log viewer           │◀──│                      │   │  Kibo check + GXO event  │
 └──────────────────────┘   └──────────────────────┘   └──────────────────────────┘
```

The two queues share nothing, so both workflows can run at the same time.

| Tool | Queue | Workflow | Worker |
|---|---|---|---|
| Kibo cancellations | `input/` `archive/` `error/` `logs/` | `cancel_shipments.yml` | `scripts/process_cancellations.py` |
| Warehouse cancellations | `warehouse/input/` `warehouse/archive/` `warehouse/error/` `warehouse/logs/` | `cancel_warehouse_shipments.yml` | `scripts/process_warehouse_cancellations.py` |

---

## Repository layout

```
.github/workflows/cancel_shipments.yml             push + manual trigger (Kibo)
.github/workflows/cancel_warehouse_shipments.yml   push + manual trigger (warehouse)
.github/workflows/reconcile_cancellations.yml      hourly + manual trigger (follow-up)
scripts/process_cancellations.py                   the Kibo worker
scripts/process_warehouse_cancellations.py         the warehouse worker
scripts/reconcile_cancellations.py                 the reconciliation worker
scripts/cancellation_log.py                        the cancellation ledger, shared
sql/warehouse_orders_cancellation_table.sql                  the ledger's columns; run once
input/ archive/ error/ logs/                       the Kibo queue
warehouse/input/ warehouse/archive/ …              the warehouse queue
examples/cancellations_example.csv
examples/warehouse_cancellations_example.csv
requirements.txt                                   worker dependencies, all workers
```

The consoles are in the **applications** repository; `app/` and
`tools/make_password_hash.py` moved there.

## Setup

### 1. The repository

Push this tree to a **private** repository. The CSVs and logs carry order
numbers and, in the Kibo error responses, occasionally customer data.

GitHub Free gives 2,000 Actions minutes a month on private repositories. A run
that finds nothing in `input/` exits in a few seconds and barely consumes any
of that; see *Costs and limits* below.

### 2. Repository secrets

`Settings → Secrets and variables → Actions → New repository secret`:

| Secret | Used by | Value |
|---|---|---|
| `KIBO_API_CREDS` | both workers | `{"client_id": "...", "client_secret": "..."}` |
| `BI_BIGQUERY_CREDS` | Kibo, warehouse, reconciliation | The full service-account JSON for `tlg-business-intelligence-prd` |
| `WLFS_BIGQUERY_CREDS` | warehouse, reconciliation | The full service-account JSON for `tlg-wlfs-prd` |
| `GXO_API_KEY` | warehouse, reconciliation | The endpoint key: the raw key, or the whole `GXO_endpoint_key.json` |

The warehouse worker and the reconciliation worker both write to
`tlg-business-intelligence-prd`.til.warehouse_orders_cancellation_table, so the
`BI_BIGQUERY_CREDS` service account needs `roles/bigquery.dataEditor` on that
dataset, not only read access.

Optional repository variables: `KIBO_TENANT_MAP` (merged over the built-in brand
map, used by both workers) and `GXO_URL` (to point the warehouse worker at a
test endpoint).

Paste the JSON exactly as it is, newlines and all. Without `BI_BIGQUERY_CREDS`
the worker still runs: it skips the BigQuery lookup and sends every record
through the Kibo fallback, which only works for rows that carry an
`orderNumber` (or a `shipmentOrderNumber` it can derive one from).

### 3. The web app

The consoles live in the **applications** repository and are deployed once, on
Streamlit Community Cloud, from `streamlit_app.py` at its root. Its README has
the steps. The only thing it needs from here is a **fine-grained personal
access token** scoped to this repository with `Contents: Read and write`: it
commits CSVs to the queues and reads the logs, nothing else.

---

# 1. Kibo cancellations

A CSV with `shipmentOrderNumber`, `productId` and `orderNumber` lands in `input/`.
The workflow reads it and, for each row:

| Row | What happens |
|---|---|
| `shipmentOrderNumber` + `productId` | Looks the shipment order number up in `tlg-business-intelligence-prd.til.oms_active_shipments`. When several rows match, keeps the one whose `shipment_json` carries that product. Cancels **only that item** with `PUT /api/commerce/shipments/{n}/canceledItems`. |
| `shipmentOrderNumber`, no `productId` | Same lookup, then cancels **every** matching `oms_shipment_number` with `PUT /api/commerce/shipments/{n}/canceled`. |
| `orderNumber` only | Looks it up on `external_order_number`, then cancels every matching shipment. |
| Nothing found in BigQuery | Falls back to `GET /api/commerce/shipments?filter=externalOrderId=={orderNumber}` and works from that response. |

Then it writes `logs/run-<stamp>.log` and `logs/run-<stamp>.csv`, and moves the
input file to `archive/` (everything succeeded or had nothing to do) or
`error/` (the file was unreadable, or a record failed).

## Using it

These sections describe the *Kibo cancellations* page of the applications app.

**Upload a CSV.** Drop in a file with any of these column names (case, spaces
and underscores are ignored): `shipmentOrderNumber`, `productId`, `orderNumber`,
`quantity`, `reasonCode`, `moreInfo`, `tenantId`, `siteId`. The app shows what
it parsed before anything is committed.

**One-off cancellation.** The *Single cancellation* tab turns one item, one
shipment or one order into the same one-row CSV and drops it in `input/`.

**Logs.** The *Logs* tab lists every `logs/run-*.log`, newest first. Each log
opens with a count and then lists, per file, what succeeded, what was skipped
and what failed, with the reason.

**Folders.** Shows how many files are waiting, archived or in error.

### Running the workflow by hand

`Actions → Cancellations → Run workflow`. The useful input is **dry run**: it
resolves every record, reads each shipment from Kibo and reports exactly what it
would cancel, without sending a single write. Worth doing the first time, and
any time a file looks unusual.

## How a record is judged

| Outcome | Meaning | File goes to |
|---|---|---|
| `success` | Kibo accepted the cancellation | archive |
| `already_terminal` | The shipment was already CANCELED, FULFILLED, SHIPPED… | archive |
| `item_already_canceled` | The product is already in `canceledItems` | archive |
| `dry_run` | Nothing was sent | input (nothing moves on a dry run) |
| `failed` | Not found, no `lineId`, an HTTP error, a malformed row | error |

A failed record does **not** fail the workflow run. The job stays green, the log
says what went wrong, and the file lands in `error/` for someone to look at. If
you would rather have a red run, change the final `return 0` in
`process_cancellations.py` to `return 2 if failed else 0`.

## Things worth knowing before you rely on it

**The item endpoint is the right one.** `PUT …/canceledItems` takes
`{blockAssignment, holdBlockAssignment, isUserAction, items[], sendNotifications}`
where each item is `{lineId, quantity, canceledReason}`. `PUT …/canceled`
cancels the whole shipment. The worker always reads the shipment from Kibo first
to get the real `lineId` and the available quantity, because `shipment_json` in
BigQuery is a snapshot and can be stale.

**`productId` matching is deliberately broad.** It is compared against
`productCode`, `upc`, `sku`, `variationProductCode`, `partNumber`,
`originalOrderItemId` and the `options` values, case-insensitively and ignoring
spaces and hyphens. In your sample shipment that means `518055266024613` and
`OMIE00BC99MAT0011001` both resolve to the same line. If a product matches more
than one line on a shipment, every matching line is cancelled.

**The tenant comes from the brand prefix.** Every number opens with a
two-letter brand code — `OW-USA00037769` and `OW-US-00037769` are both `OW` —
and that code picks the Kibo tenant. The map lives in `BRAND_TENANTS` at the top
of `scripts/process_cancellations.py` (and, for the upload preview only, in
`shared/console.py` in the applications repo). Resolution order per record:

1. a `tenantId` column on the CSV row
2. `tenant_id` from `oms_active_shipments`
3. the brand prefix, through the map
4. `--default-tenant-id`, which is **empty** by default

An unknown prefix with no default fails the record rather than cancelling
against the wrong brand. To add a brand without editing code, set a repository
variable `KIBO_TENANT_MAP` to something like `{"XY": "40001"}`; the workflow
passes it through and it is merged over the built-in map. The app flags an
unknown prefix in the upload preview and refuses it on the single-cancellation
form, so a typo is caught before anything is queued.

The current map:

| | | | | | | |
|---|---|---|---|---|---|---|
| DG 29916 | LN 29917 | MO 29988 | SC 29989 | CO 29990 | RC 29990 | WL 29991 |
| SW 29992 | CA 29993 | CV 30014 | HB 30014 | BA 30014 | RL 30016 | OW 30016 |
| FA 30017 | VW 30017 | DJ 30018 | AT 30019 | LA 30019 | PL 30020 | CL 30020 |
| HE 30021 | FE 30022 | CR 30022 | FO 30023 | EF 30023 | PJ 30475 | FU 32145 |
| WF 32147 | ZH 32147 | VE 32147 | BO 33692 | AL 33693 | MA 33693 | PA 33693 |
| FL 33693 | BB 33694 | TM 54516 | | | | |

**Site id is optional.** BigQuery supplies `site_id` when it has one, and the
Kibo fallback reads it off the shipment. When nothing supplies one, the
`x-vol-site` header is simply omitted rather than guessed. Set it per row with a
`siteId` column, or globally with `--default-site-id` / `KIBO_SITE_ID`, if a
call ever needs it.

**The derived `externalOrderId` is an assumption.** When a row has only a
`shipmentOrderNumber` and BigQuery has nothing, the worker derives the
externalOrderId by replacing the 6th character with a hyphen:
`OW-USA00037769` → `OW-US-00037769`, which is how the two line up in your sample.
Verify this holds for every brand before trusting it, and pass
`--no-derive-external-order-id` if it does not.

**Concurrency.** The workflow uses a `concurrency` group so two runs never
process `input/` at once, and the commit step rebases and retries up to five
times if someone pushes in between.

**`[skip ci]`** is in the bot's commit message so the workflow's own commit to
`input/`, `archive/` and `logs/` does not trigger another run. Pushes made with
`GITHUB_TOKEN` do not trigger workflows anyway, but the marker makes it explicit.

**Repository growth.** Git keeps every version of every archived CSV and log
forever. At a few files a day this is fine for years. If the volume grows, add a
scheduled job that deletes `archive/` and `logs/` entries older than, say, 90
days.

**Scheduled runs drift.** GitHub's cron is best effort and `*/15` can arrive
several minutes late under load. The `push` trigger on `input/**` means an
upload from the app usually starts a run within seconds anyway.

**The app sleeps.** Streamlit Community Cloud hibernates an app after a long
idle period; the first visitor wakes it, which takes about half a minute. The
workflow is unaffected.

**Password security.** The logins are a shared secret checked against PBKDF2
hashes — good enough to keep the page off the open internet, not a substitute
for real accounts. Anyone with a login can cancel anything the Kibo credentials
can reach, from any IP. Keep the list short and rotate it when people leave.

## Local run of the Kibo worker

```bash
pip install -r requirements.txt
export KIBO_API_CREDS='{"client_id":"...","client_secret":"..."}'
export BI_BIGQUERY_CREDS="$(cat service-account.json)"
cp examples/cancellations_example.csv input/
python scripts/process_cancellations.py --dry-run
```

Useful flags: `--limit N`, `--reason-code`, `--more-info`,
`--no-send-notifications`, `--host`, `--max-retries`, `--sleep-between-requests`,
`--tenant-map`, `--default-tenant-id`, `--default-site-id`.

---

# 2. Warehouse cancellations

The warehouse counterpart. Its queue is `warehouse/input/`, and everything
it writes stays under `warehouse/`.

## What it does

A CSV with `shipmentOrderNumber` and, optionally, `productId` lands in `warehouse/input/`.
The workflow reads it and, for each row:

| Row | What happens |
|---|---|
| `shipmentOrderNumber`, no `productId` | The whole shipment is cancelled at the warehouse. Kibo is never called. |
| `shipmentOrderNumber` + `productId`, that product is the **only** product on the shipment | The warehouse cancellation is sent, exactly as above. |
| `shipmentOrderNumber` + `productId`, the shipment holds **other products too** | **Nothing happens**, at the warehouse or in Kibo. The record is reported as `has_other_products` and left for a human. |
| Always, before anything is sent | Kibo is read for the order and the shipments carrying this `shipmentOrderNumber` are counted; more than one still in play and the record is refused without cancelling anything. See below. |
| Whenever the whole shipment is to be cancelled | `tlg-wlfs-prd.til.prd_orders` (joined to `til.prd_brandConfig`) supplies `logisticOrderNumber`, `locationName`, `brand` and `brandCode` for the `%GX%` locations, and one event is POSTed per matching row. |
| `cancelInKibo` says **yes**, and the run allows the Kibo step | After the warehouse has accepted the event, the whole shipment is cancelled in Kibo too. See below. |

Then it writes `warehouse/logs/run-<stamp>.log` and `warehouse/logs/run-<stamp>.csv`, and moves the
input file to `warehouse/archive/` (everything succeeded or had nothing to do) or
`warehouse/error/` (the file was unreadable, or a record failed).

### When one number means two shipments

A `shipmentOrderNumber` is meant to identify one shipment. Sometimes it does
not, and then nothing in the row says which one was meant — so the worker
cancels **neither**.

Before any event leaves for the warehouse, every record is read back from Kibo
with `GET /api/commerce/shipments?filter=externalOrderId=={orderNumber}`, and
the shipments carrying the row's `shipmentOrderNumber` are counted. How many of
them are still in play depends on the channel, `items[].data.channel`:

| `items.data.channel` | A shipment is still in play when |
|---|---|
| `Farfetch` | its `shipmentStatus` is **not** `CANCELED`, `FULFILLED` or `REASSIGNED` |
| anything else | its `shipmentStatus` is `READY` **and** its location name contains `GX` |

More than one still in play and the record is reported as `ambiguous`: **no
CANCEL event to the warehouse and no cancellation in Kibo**. The log names each
of them as `shipmentNumber[status@location, channel]`, and the file lands in
`warehouse/error/`, so the cancellation can be redone by `shipmentNumber`, one
at a time.

The check runs for every record — with or without a `productId`, with or
without the Kibo step — so `KIBO_API_CREDS` is now needed for every run. If the
shipments cannot be read at all the record is reported as `failed` and nothing
is cancelled: a duplicate that could not be ruled out is treated as a
duplicate.

### Cancelling in Kibo as well

Every row cancels at the warehouse. The `cancelInKibo` column decides whether
Kibo follows:

| `cancelInKibo` | What happens |
|---|---|
| empty, `no`, `false`, `0` | The warehouse only. Kibo is left untouched. This is the default. |
| `yes`, `true`, `1` | The warehouse **first**; then `PUT /api/commerce/shipments/{shipmentNumber}/canceled` for the one live shipment carrying that `shipmentOrderNumber`. |

`--cancel-in-kibo` flips the default for rows that leave the column empty; a row
that says `yes` or `no` always decides for itself.

**The Kibo step is off unless the run switches it on.** `--allow-kibo` is the
switch, and without it a row asking for Kibo is cancelled at the warehouse on
its own — the same end state as *Warehouse only* — with its Kibo half recorded
as `kibo_disabled`. In the workflow the switch is the `allow_kibo` checkbox on
a manual run, and the `ALLOW_KIBO_CANCELLATIONS` repository variable (set it to
`true`) on a push; unset means off. So a console user can choose *Warehouse,
then Kibo* today and the Kibo half stays dormant until the variable is turned
on.

The order is the point. The warehouse is what is physically moving the goods,
so it is told first, and Kibo is only brought in line once the warehouse has
accepted the event. **A record whose warehouse event failed is never cancelled
in Kibo** — it is reported as `not_sent` on the Kibo row, and the warehouse row
carries the actual failure. The reverse (Kibo cancelled, warehouse still
packing) is the one state this tool will not produce.

The Kibo shipment is found the same way the product check finds it, through
`GET /api/commerce/shipments?filter=externalOrderId=={orderNumber}`, so the
`orderNumber` rules below apply to this step too. The lookup is by order, so
the shipments have to name themselves: if none of them carries the row's
`shipmentOrderNumber`, nothing is cancelled and the record fails rather than
the worker cancelling whatever came back. A shipment Kibo already
reports as cancelled, closed, shipped or otherwise terminal is left alone and
reported as `already_terminal`. The cancellation records `reasonCode` (from the
row, else `--kibo-reason-code`, default `CustomerChangedTheirMind`) and
`moreInfo` (from the row's `moreInfo`, else its `reasonMessage`, else
`--kibo-more-info`). The *Warehouse cancellations* page asks for both, and only
when *Warehouse, then Kibo* is chosen — there is nothing for them to record
otherwise. It cancels whole shipments only; a single item is
`process_cancellations.py`'s job.

### Why a product splits the two paths

The GXO event has no item granularity. Its `products` array is **always empty**,
exactly as in `cancellation_api_sender_to_GXO.py`, and the warehouse reads that
as "stop this whole shipment". So a product can only ever decide *whether* to
stop the shipment, never *what* to stop inside it.

That leaves one sensible rule, and it is the rule the worker follows: if the
product is the whole shipment, stopping the shipment and cancelling the product
are the same act, so the warehouse gets the event. If the shipment holds
anything else, nothing is sent and the record is reported, because cancelling
would also stop products nobody asked about. The product check itself only ever
reads from Kibo; the only write this worker makes to Kibo is the cancellation a
row asks for with `cancelInKibo`, and that one happens after the warehouse
event, never before it.

The sent payload:

```json
{
  "eventType": "CANCEL",
  "eventClass": "SHIPMENT",
  "eventId": "<uuid4, upper case>",
  "eventTimestamp": "2026-11-15T09:40:19.956Z",
  "data": {
    "shipmentOrderNumber": "OW-USA00037769",
    "logisticOrderNumber": "...",
    "locationCode": "<locationName from prd_orders>",
    "brandCode": "...",
    "brand": "...",
    "products": [],
    "updateReason": "external",
    "reasonMessage": "Cancelled"
  }
}
```

## Using it

**Upload a CSV.** Column names are matched ignoring case, spaces and
underscores: `shipmentOrderNumber` (required), `productId`, `orderNumber`,
`reasonCode`, `moreInfo`, `cancelInKibo`, `reasonMessage`, `tenantId`, `siteId`.
The *Warehouse cancellations* page shows what it parsed before anything is
committed, including the `cancelInKibo` it is about to queue.

**The warehouse event's reason is fixed.** `data.reasonMessage` goes out as
`Cancelled` on every event: the console does not ask for one, because the
warehouse does nothing with a per-shipment explanation. A CSV committed by hand
can still carry a `reasonMessage` column, and `--reason-message` changes the
default for a whole run.

**One-off cancellation.** The *Single cancellation* tab turns one shipment —
with or without a product condition — into the same one-row CSV.

**Which systems to cancel in.** Both tabs of the page open on the same choice:
*Warehouse only* or *Warehouse, then Kibo*. It fills the `cancelInKibo` column
for every row of the file, so one upload is one decision. A CSV written by hand
and committed straight to `warehouse/input/` can still set the column row by
row; the worker reads it either way.

**Logs.** The *Logs* tab lists every `warehouse/logs/run-*.log`, newest first.

### Running the workflow by hand

`Actions → Warehouse cancellations → Run workflow`. The useful input is **dry
run**: it resolves every record, runs the product check, and reports exactly
what it would send, without a single POST to GXO. Worth doing the first time.

## How a record is judged

| Outcome | Meaning | File goes to |
|---|---|---|
| `success` | GXO accepted the event | archive |
| `has_other_products` | The shipment holds products beyond the one named; nothing was sent | archive |
| `already_terminal` | The Kibo step found every matching shipment already cancelled, closed or shipped | archive |
| `kibo_disabled` | The row asked for Kibo, but the Kibo step was switched off for the run; the warehouse cancellation went out on its own | archive |
| `ambiguous` | More than one shipment still in play answers to the `shipmentOrderNumber`; **nothing was cancelled**, at the warehouse or in Kibo | error |
| `not_sent` | The Kibo step was held back because the warehouse event failed | error (the warehouse row is the failure) |
| `dry_run` | Nothing was sent | warehouse/input (nothing moves on a dry run) |
| `failed` | No GX row in BigQuery, the product is not on the shipment, no tenant, an HTTP error, a malformed row, or the Kibo cancellation was refused | error |

Every record is logged with the stage it belongs to — `[warehouse]` or `[kibo]`
— so a row that asked for both shows up twice, in the order they ran.

A failed record does **not** fail the workflow run. The job stays green, the log
says what went wrong, and the file lands in `warehouse/error/`.

## Things worth knowing before you rely on it

**The `orderNumber` for the product check.** Reading a shipment from Kibo needs
an `externalOrderId`. A row can carry one in `orderNumber`; otherwise the worker
derives it from the `shipmentOrderNumber` by replacing the 6th character with a
hyphen (`OW-USA00037769` → `OW-US-00037769`), the same assumption the Kibo
worker makes. Pass `--no-derive-external-order-id` if it does not hold for a
brand.

**Matching the shipment.** Shipments in the `getShipments` response are matched
on `data.shipmentOrderNumber`. When no shipment in the response carries that
field, every shipment of the order is judged together instead of guessing — a
stricter reading, which can only ever block more.

**`productId` matching is deliberately broad.** It is compared against
`productCode`, `upc`, `sku`, `variationProductCode`, `partNumber`,
`originalOrderItemId` and the `options` values, case-insensitively and ignoring
spaces and hyphens.

**The tenant comes from the brand prefix**, and only matters for product-gated
rows. Resolution order: a `tenantId` column, the prefix through `BRAND_TENANTS`,
then `--default-tenant-id` (empty by default). An unknown prefix fails the
record rather than reading another brand's tenant.

**Read-only in Kibo.** The product check is a `GET`. Cancelling an item in Kibo
is the other tool's job, so a shipment that holds other products is reported
here and dealt with there if that is what you want.

**Several GX rows per shipment.** `prd_orders` can hold more than one `%GX%`
location row for one `shipmentOrderNumber`. One event is sent per row, as the
original script did.

**TLS.** The GXO endpoint negotiates legacy renegotiation and presents a chain
the runner cannot verify, so the worker sets `OP_LEGACY_SERVER_CONNECT` and
turns verification off for that host — the same compromise the desktop script
makes. Nothing else in the worker runs with verification off.

## Local run of the warehouse worker

```bash
pip install -r requirements.txt
export WLFS_BIGQUERY_CREDS="$(cat service-account.json)"
export GXO_API_KEY="$(cat GXO_endpoint_key.json)"
export KIBO_API_CREDS='{"client_id":"...","client_secret":"..."}'
cp examples/warehouse_cancellations_example.csv warehouse/input/
python scripts/process_warehouse_cancellations.py --dry-run
```

Useful flags: `--limit N`, `--dry-run`, `--no-product-check`,
`--no-derive-external-order-id`, `--reason-message`, `--update-reason`,
`--gxo-url`, `--host`, `--max-retries`, `--sleep-between-requests`,
`--tenant-map`, `--default-tenant-id`, `--default-site-id`.

---

# 3. The cancellation ledger and the reconciliation workflow

Everything the warehouse worker cancels is written down in

```
`tlg-business-intelligence-prd`.til.warehouse_orders_cancellation_table
```

and a third workflow, **Reconcile cancellations**, works that table afterwards:
it confirms what the warehouse acknowledged, sends again what it did not, and
cancels the confirmed ones in Kibo. It reads nothing but the table, so it shares
no folder and no file with the other two workflows and runs beside them.

Run **`sql/warehouse_orders_cancellation_table.sql`** once in the BI project
before any of this. It creates the table with `CREATE TABLE IF NOT EXISTS` and
then adds anything a previous deployment left missing with
`ADD COLUMN IF NOT EXISTS`, so it is safe to run twice and leaves every existing
column alone.

**This is not the older `til.orders_cancellation_table`.** That table belongs to
the `shipments_resolver` repository, carries its own `reason_code` and
`more_info` columns, and is not read or written by anything here. Nothing in
this repository touches it, and nothing there touches this one.

## What a row says

| Column | Written by | Meaning |
|---|---|---|
| `row_insertion_date` | warehouse worker | when the row was first inserted |
| `row_last_modified_date` | both | when any column of the row last changed |
| `shipment_order_number` | warehouse worker | the key; one row per shipment |
| `shipment_items` | warehouse worker | the shipment's products, joined by `;` |
| `cancellation_sent_to_warehouse` | both | `false` when the GXO call failed, `true` when it went through |
| `cancellation_sent_to_warehouse_date` | both | when it was sent, or attempted |
| `cancellation_confirmed_by_warehouse` | reconciliation | `true` once the warehouse's own CANCELLED event carries exactly these items |
| `cancellation_confirmed_by_warehouse_date` | reconciliation | that event's `eventTimestamp` |
| `number_of_cancellations_sent_to_warehouse` | both | `1` on the first successful send, `+1` on every resend |
| `cancelled_on_kibo` | reconciliation | `true` once the Kibo shipment is cancelled |
| `cancelled_on_kibo_date` | reconciliation | when that happened |

The key is `shipment_order_number`. A shipment cancelled a second time does not
get a second row; it gets its counter raised, which is what the counter is for.

`shipment_items` holds one identifier per item line — the first of
`productCode`, `upc`, `sku`, … that the Kibo item carries — upper-cased, sorted
and joined by `;`. That is the same spelling the `shipments_resolver` scripts
use for their own table, which split such a column on `[,\s;|]+`, so the format
is readable by either if the two are ever pointed at each other.

### What the warehouse worker writes

Every record that named a shipment gets a row, whether or not the CANCEL event
went out: a cancellation refused as `ambiguous` or `has_other_products` is worth
being able to look up later, and `cancellation_sent_to_warehouse` is `true` only
when the warehouse actually took the event. `cancelled_on_kibo` is `false`
unless that same run also cancelled in Kibo (the `--allow-kibo` path), in which
case the row says so straight away and the reconciliation leaves it alone.

**The ledger can never break a cancellation.** A run that cannot reach the table
cancels anyway, says so once per record in its log, and ends green — a shipment
that was stopped at the warehouse is still stopped, whether or not it was
written down. `--no-ledger` switches the writing off for a run.

## The three stages

### 1. Confirm

Every row with `cancellation_sent_to_warehouse = true` and
`cancellation_confirmed_by_warehouse = false` is looked for in the warehouse's
own event feed:

```sql
SELECT shipmentOrderNumber, eventType, eventTimestamp, requestPayload
FROM `tlg-wlfs-prd`.til.prd_shipmentStatus
WHERE shipmentOrderNumber IN UNNEST(@numbers)
  AND UPPER(eventType) IN ('CANCELLED', 'CANCELED')
```

A row is confirmed when one of those events carries **exactly** the items in
`shipment_items`: every item of the event is in the column and every item of the
column is in the event, not more and not less. An event listing a superset or a
subset is about a different shipment of the same order and confirms nothing.
Identifiers are compared upper-cased and blind to spaces and hyphens, so the
warehouse's spelling of a code and Kibo's spelling of it still match.

`cancellation_confirmed_by_warehouse_date` is the event's own `eventTimestamp`,
not the time the reconciliation noticed it.

### 2. Resend

A row still unconfirmed whose `cancellation_sent_to_warehouse_date` is at least
24 hours old (`--resend-after-hours`) gets the same CANCEL event again, built
from the same `prd_orders` GX rows the warehouse worker uses. On success:

```
number_of_cancellations_sent_to_warehouse += 1
cancellation_sent_to_warehouse_date        = now
```

Stage 1 runs first, so a cancellation the warehouse has acknowledged in the
meantime is never sent twice.

### 3. Cancel in Kibo

Every row with `cancellation_confirmed_by_warehouse = true` and
`cancelled_on_kibo = false` is closed in Kibo. The shipment is found the long
way round, because the Kibo Shipment API is queried by order and not by
shipmentOrderNumber:

```
`tlg-wlfs-prd`.til.prd_orders   shipmentOrderNumber -> orderNumber
GET /api/commerce/shipments?filter=externalOrderId=={orderNumber}
PUT /api/commerce/shipments/{shipmentNumber}/canceled
```

Of the shipments that come back, only the ones whose `shipmentStatus` is
`READY`, `BACKORDER` or `CUSTOMER_CARE` (`--cancel-target-statuses`) are
cancelled, and by default only those carrying exactly the row's
`shipment_items`.

**Why the items and not the number.** The `shipmentOrderNumber` does not
identify one shipment, in either of the two shapes the data comes in:

- the Farfetch orders repeat it verbatim across every generation — one real
  `getShipments` response carries three shipments, `770126` (`READY`), `766738`
  and `770107` (both `REASSIGNED`), and all three say
  `data.shipmentOrderNumber = DG-FF-DUW191008089`, so matching on the number
  matches all three;
- elsewhere the live generation carries a later letter in 6th position —
  `DJ-USA…` becomes `DJ-USB…` — so matching on the row's own number matches
  nothing.

The items survive the reassignment either way, so they are what picks the
shipment out. `--require-shipment-order-number` matches on the number instead,
and `--no-item-match` drops the gate and cancels every shipment of the order in
one of those statuses — neither is the default, and both are worth thinking
twice about.

**Where a shipment keeps its items.** Kibo empties `items` when it hands the
goods to the next generation and moves them to `reassignedItems`; in that same
response, `766738` and `770107` have `"items": []` with the product under
`reassignedItems`, while the live `770126` has it under `items`. The gate reads
`items` first and falls back to `reassignedItems` / `canceledItems`, so a
shipment parked in `CUSTOMER_CARE` with its items already moved is still
matched. The reassigned ancestors cannot be picked up that way, because the
status filter has already ruled them out.

The ledger's `shipment_items` is written from `items` only, with no fallback: the
shipments of one order include the generations Kibo has already reassigned, and
reading their moved items would put products on the row that the live shipment no
longer carries.

A row whose `shipment_items` is empty is skipped rather than guessed at.

## Using it

The workflow runs **hourly at :17** and on demand from
`Actions → Reconcile cancellations → Run workflow`, where every stage can be
skipped and every threshold changed for one run. A dry run resolves and reports
and writes nothing, anywhere.

Each run leaves `warehouse/logs/reconcile-<stamp>.log` and `.csv`, one line per
row, and the job stays green even when a row fails so one bad shipment does not
look like a broken workflow.

## Things worth knowing before you rely on it

- **`cancel_cancelled_shipments.py` in the `shipments_resolver` repository can
  still reach the same shipments.** It reads a different table
  (`til.orders_cancellation_table`) and finds its work from
  `oms_active_shipments`, so the two never share a row — but a shipment
  cancelled at the warehouse can appear in both paths, and both end in a Kibo
  cancellation. Whichever gets there first cancels; the other finds the shipment
  already `CANCELED` and skips it. Nothing is cancelled twice, but the two logs
  will disagree about who did what, and only this repository's ledger will show
  `cancelled_on_kibo = true`. If both run against the same brands, pick one as
  the owner of stage 3 and switch the other off (`--skip-kibo` here, or the
  schedule there).
- **The resend is a real cancellation.** The warehouse has no idea it has seen
  this event before; it is a fresh CANCEL for a shipment it may already have
  stopped. That is deliberate — the event is the only way to ask again — but it
  is why the threshold is a day and not an hour.
- **A row with no `shipment_items` can never be confirmed**, and so is resent
  every day forever. Those rows are reported as `skipped` with that reason in
  stage 1; watch for them. They come from records whose Kibo read failed when
  the warehouse worker wrote them.
- **The brand's Kibo site matters as much as its tenant.** A wrong `x-vol-site`
  scopes the query to a site holding none of the order's shipments and Kibo
  answers `200` with an empty page, which reads exactly like "this order has no
  shipments". `BRAND_KIBO_SITES` in `scripts/reconcile_cancellations.py` is the
  authority, kept in step with `BRAND_KIBO_CONFIG` in `shipments_resolver`.
- **`cancelled_on_kibo` is set once per row, not once per shipment.** When the
  item gate matches two shipments of the same order, both are cancelled and the
  one row is flagged.

## Local run of the reconciliation worker

```bash
pip install -r requirements.txt
export BI_BIGQUERY_CREDS="$(cat bi-service-account.json)"
export WLFS_BIGQUERY_CREDS="$(cat wlfs-service-account.json)"
export KIBO_API_CREDS='{"client_id":"...","client_secret":"..."}'
export GXO_API_KEY="$(cat GXO_endpoint_key.json)"
python scripts/reconcile_cancellations.py --dry-run
```

Useful flags: `--dry-run`, `--limit N`, `--resend-after-hours`,
`--cancel-target-statuses`, `--no-item-match`,
`--require-shipment-order-number`, `--skip-confirm`, `--skip-resend`,
`--skip-kibo`, `--kibo-reason-code`, `--kibo-more-info`, `--host`,
`--tenant-map`, `--default-tenant-id`, `--default-site-id`.

---

## Costs and limits

| | |
|---|---|
| GitHub Actions | 2,000 min/month free on private repos. A no-op run is ~10s; a run with work is 1–2 min. |
| Streamlit Community Cloud | Free. Unlimited public apps, a small number of private ones, ~1 GB RAM. |
| BigQuery | The lookup is one query per run, filtered to the numbers in the files. `shipment_json` is only selected when a row carries a `productId`. The ledger adds one `MERGE` per record, and the reconciliation one schema read plus three queries per run, plus one small `UPDATE` per row it changes. |
| Kibo | One GET plus one PUT per shipment, with retry on 429/5xx. |

Free-tier terms change; check the current ones when you set this up.
