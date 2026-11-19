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
| `sync.py` | Runs a provider: lists purchases, fetches new ones, archives the raw JSON, stores normalized rows, looks up products, repairs quantities, records the run and triggers alerts. |
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

The raw JSON of every purchase is kept under `<data dir>/raw/<retailer>/`, so new parsing can be applied
later without fetching again (`groceries sync --full` re-fetches everything the retailer still has).

## Browser login flow

1. `POST /api/connections/<retailer>/start` creates a random state (and for Jumbo a PKCE verifier), keeps
   it in the secrets directory for 15 minutes, sets an HttpOnly SameSite=Strict cookie with the state and
   returns the retailer's authorize URL.
2. The user logs in on the retailer's page. The retailer tries to hand a one-time code to its phone app.
3. The connect helper (bookmarklet) reads or requests that code in the retailer's tab and opens
   `/connect/callback?provider=…#code=…`. The code is in the URL fragment, so it never reaches the server
   or its access log.
4. The callback page asks the user to confirm, then posts the code to `/api/connections/<retailer>/finish`.
   The server only accepts it from a same-origin JSON request carrying the state cookie, exchanges it for
   tokens and stores them.

All state-changing endpoints require same-origin JSON POSTs, and the app only answers to known host
names (DNS-rebinding protection).

## Concurrency

- One sync per retailer at a time: `sync.run_sync` holds `<data dir>/sync-<retailer>.lock`.
- Token refreshes and disconnects share `<secrets dir>/<retailer>-token.lock`, so a refresh can never
  resurrect a login that was just disconnected.
- The web server opens SQLite read-only; SQLite's WAL mode lets it read while a sync writes.
