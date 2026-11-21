# Architecture

dutchgroceries is a small Python package (`src/groceries`) with three entry points, all in `cli.py`:
`groceries sync` (fetch data), `groceries serve` (dashboard) and a few import and admin commands. There
is no background worker inside the web server; syncs run from a timer, cron, the Docker `sync`
container, or the dashboard's *Sync now* button (a thread in the web process).

```
retailer app APIs ──► providers/ah.py, providers/jumbo.py ──► sync.py ──► SQLite + raw JSON
                                                                           │
                       browser ◄── static/index.html ◄── web.py (JSON API) ◄── analytics.py
```

## Modules

| Module | Role |
|---|---|
| `config.py` | All settings, read from `GROCERIES_*` environment variables (optionally from `GROCERIES_ENV_FILE`). |
| `providers/base.py` | The `Provider` interface and the normalized records (`Purchase`, `Line`, `Discount`, `Payment`, `ProductInfo`). |
| `providers/ah.py` | Albert Heijn: GraphQL receipts, online orders, catalogue lookups, token handling. |
| `providers/jumbo.py` | Jumbo: Auth0 PKCE login, digital receipts parsed from the printed receipt document, order totals. |
| `tokens.py` | `RotatingTokenStore`: tokens on disk, refreshed under a file lock and written atomically before use. |
| `accounts.py` | Store accounts (several per retailer): labels, token file paths, duplicate detection, the upgrade from single-account installs. |
| `sync.py` | Runs a provider: lists purchases, fetches new ones, archives the raw JSON, stores normalized rows, looks up products, repairs quantities, records the run and triggers alerts. |
| `reimport.py` | `groceries reimport` and `export-raw`: re-parse the archived raw JSON offline, and redact a raw file for sharing. |
| `db.py` | The SQLite schema (created and migrated on connect). |
| `analytics.py` | Read-only queries behind the dashboard, including the cleaning of public shelf prices. |
| `web.py`, `web_connect.py` | FastAPI app: JSON API, dashboard page, and the browser login flow. |
| `connect.py` | Server side of the browser login: pending logins, state checks, status, disconnect. |
| `notify.py` | Optional ntfy/webhook alerts, one per outage. |
| `importers/checkjebon.py`, `importers/bank.py` | Optional backfills: public shelf prices, bank CSV payments. |
| `demo.py` | Synthetic data for demos and screenshots. |
| `static/index.html`, `static/helper.js` | The single-page dashboard (ECharts) and the connect-helper bookmarklet. |

## Data model

Every row carries a `retailer` slug (`ah`, `jumbo`).

- `purchases`: one receipt or online order (`channel` = `store` / `online`), with totals, store and dates.
  `ts_utc` is the moment in UTC; `local_date` is the day in `GROCERIES_TZ`.
- `purchase_lines`: items, with quantity, weight, per-item or per-kg price, the line amount before
  receipt-level discounts, and a `product_key` (the retailer's product id, or the printed name for Jumbo).
- `discounts`, `payments`: per purchase.
- `products`: catalogue details per product key (title, brand, category; `category_inferred` when it
  was guessed from a search).
- `market_prices`, `market_scans`: optional public shelf prices.
- `bank_payments`: optional supermarket payments from bank CSVs.
- `sync_runs`: one row per sync (`ok`, `partial`, `error`, `aborted`).

The raw JSON of every purchase is kept under `<data dir>/raw/<retailer>/<channel>/`, so new parsing can be
applied later without fetching again: `groceries reimport` (`reimport.py`) re-parses those files with each
provider's `parse_raw()` and rewrites only the purchases whose result changed. `groceries sync --full`
re-fetches everything the retailer still has.

## Accounts

A retailer can have several store accounts (a household). Each is a row in `accounts` (label, masked
e-mail, a SHA-256 *identity* of the retailer's member e-mail or user id) plus one token file
`<secrets dir>/<retailer>-<id>-token.json`. Purchases carry the `account_id` that first synced them; the
purchase id stays `<retailer>:<channel>:<external id>`, so a receipt that two accounts can see is stored
once (the second account counts it as a duplicate and moves on). `sync.run_sync` loops over every
connected account of a retailer, records a `sync_runs` row and an alert state per account, and raises the
first error only after all accounts ran.

Upgrading from a single-account version: `db.connect` gives every retailer that has purchases an account
"Main" and assigns its purchases and sync runs; `accounts.migrate_legacy` moves `<retailer>-token.json`
to the account's file and folds `connections.json` into the table. Both are idempotent.

## Browser login flow

1. `POST /api/connections/<retailer>/start` (body: `account_id` to reconnect an account, or an optional
   `label` for a new one) creates a random state (and for Jumbo a PKCE verifier), keeps it together with
   that account slot in the secrets directory for 15 minutes, sets an HttpOnly SameSite=Strict cookie with
   the state and returns the retailer's authorize URL.
2. The user logs in on the retailer's page. The retailer tries to hand a one-time code to its phone app.
3. The connect helper (bookmarklet) reads or requests that code in the retailer's tab and opens
   `/connect/callback?provider=…#code=…`. The code is in the URL fragment, so it never reaches the server
   or its access log.
4. The callback page asks the user to confirm, then posts the code to `/api/connections/<retailer>/finish`.
   The server only accepts it from a same-origin JSON request carrying the state cookie and exchanges it
   for tokens into a staging file. It then asks the retailer who logged in: a store account that is
   already connected (or, on a reconnect, a different account than the slot's) is refused and the staging
   file deleted. Otherwise the tokens move to the slot recorded at step 1.

All state-changing endpoints require same-origin JSON POSTs, and the app only answers to known host
names (DNS-rebinding protection).

## Concurrency

- One sync per retailer at a time: `sync.run_sync` holds `<data dir>/sync-<retailer>.lock`.
- One sync per retailer covers all its accounts one after another, so two accounts never hit the
  retailer's API at the same time.
- Token refreshes and disconnects share `<secrets dir>/<retailer>-<id>-token.lock`, so a refresh can never
  resurrect a login that was just disconnected.
- The web server opens SQLite read-only; SQLite's WAL mode lets it read while a sync writes.
