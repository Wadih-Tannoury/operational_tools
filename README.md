# Kibo cancellation tool

A password-protected web page where anyone with the credentials can queue Kibo
cancellations, and a GitHub Actions workflow that applies them. No server to
run, no machine that has to stay on, nothing to pay for.

```
  browser                 GitHub repo                    GitHub Actions
 ┌────────────────┐      ┌──────────────┐               ┌─────────────────────┐
 │ Streamlit app  │ ───▶ │ input/*.csv  │ ── triggers ─▶│ process_cancellations│
 │ login + upload │      │              │               │  BigQuery lookup     │
 │ single entry   │      │ archive/     │ ◀── moves ────│  Kibo cancel         │
 │ log viewer     │ ◀─── │ error/       │               │  writes logs/        │
 └────────────────┘      │ logs/        │               └─────────────────────┘
                         └──────────────┘
```

## What it does

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

## Repository layout

```
.github/workflows/cancel_shipments.yml   schedule + push + manual trigger
scripts/process_cancellations.py         the worker
app/streamlit_app.py                     the front end
app/.streamlit/secrets.toml.example      what the front end needs
tools/make_password_hash.py              generates the login hashes
input/ archive/ error/ logs/             the folders the workflow moves files between
examples/cancellations_example.csv
requirements.txt                         worker dependencies
app/requirements.txt                     front-end dependencies
```

## Setup

### 1. The repository

Push this tree to a **private** repository. The CSVs and logs carry order
numbers and, in the Kibo error responses, occasionally customer data.

GitHub Free gives 2,000 Actions minutes a month on private repositories. A run
that finds nothing in `input/` exits in a few seconds and barely consumes any
of that; see *Costs and limits* below.

### 2. Repository secrets

`Settings → Secrets and variables → Actions → New repository secret`:

| Secret | Value |
|---|---|
| `KIBO_API_CREDS` | `{"client_id": "...", "client_secret": "..."}` |
| `BI_BIGQUERY_CREDS` | The full service-account JSON for `tlg-business-intelligence-prd` |

Paste the JSON exactly as it is, newlines and all. Without `BI_BIGQUERY_CREDS`
the worker still runs: it skips the BigQuery lookup and sends every record
through the Kibo fallback, which only works for rows that carry an
`orderNumber` (or a `shipmentOrderNumber` it can derive one from).

### 3. The web app

Streamlit Community Cloud is the free host that fits this. It needs the app
code in a repository it can read, and your data is nowhere near it, so:

1. Put **this repo** (private) wherever your data should live.
2. Either deploy the app from the same repo if your Streamlit account can read
   private repos, or copy `app/` into a small public repo of its own — it holds
   no secrets, only code.
3. On [share.streamlit.io](https://share.streamlit.io), create an app pointing
   at `app/streamlit_app.py`.
4. Under **Advanced settings → Secrets**, paste a filled-in copy of
   `app/.streamlit/secrets.toml.example`.

For the `token` field, create a **fine-grained personal access token** scoped to
the single data repository, with `Contents: Read and write`. That is all the app
needs: it commits CSVs to `input/` and reads `logs/`.

For each login, run:

```bash
python tools/make_password_hash.py alice
```

and paste the printed line under `[auth]`.

To run it locally first:

```bash
pip install -r app/requirements.txt
cp app/.streamlit/secrets.toml.example app/.streamlit/secrets.toml   # then edit it
streamlit run app/streamlit_app.py
```

## Using it

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
`app/streamlit_app.py`). Resolution order per record:

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

## Costs and limits

| | |
|---|---|
| GitHub Actions | 2,000 min/month free on private repos. A no-op run is ~10s; a run with work is 1–2 min. |
| Streamlit Community Cloud | Free. Unlimited public apps, a small number of private ones, ~1 GB RAM. |
| BigQuery | The lookup is one query per run, filtered to the numbers in the files. `shipment_json` is only selected when a row carries a `productId`. |
| Kibo | One GET plus one PUT per shipment, with retry on 429/5xx. |

Free-tier terms change; check the current ones when you set this up.

## Local run of the worker

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
