# Changelog

## v1.4.1 – 2026-11-28

- Fix: the drill-down panel showed at the bottom of the page while closed.
- Receipts and visits show each Bonus discount under the product it belongs to (matched by its cut-short
  name, e.g. `WITTEDRUIV`). Deals over several products (`ALLELUKOEK`, the organic `BIOPREMIUM`) are split
  over those products by amount; only discounts that match no product are listed separately.

## v1.4.0 – 2026-11-28

- **Click a bar to see what is behind it.** Every bar chart (spend per month or day, spend per category,
  categories over time, savings, shopping trips, average basket, price index) opens a panel with the
  products (quantity, spend, where, last bought) and the receipts behind that bar, for the selected
  period. A receipt opens with all its lines and discounts; a product opens its price history.
- **Stores visited**: click a store to see every visit with everything bought on it.
- **Long-term shelf prices**: click a product to see every price change over time (public shelf prices
  and what you paid), with a chart, the change from the previous price and since the first one.
- **Biggest price changes with a period selected** lists the products bought in that period and compares
  what you paid then with their first prices over all your history.
- **Categories for products the retailer gives none.** Jumbo's search returns no category, so every Jumbo
  product was "Onbekend". Categories are now inferred locally from Dutch grocery keywords and from similar
  categorized products, after every sync and on start (`category_inferred = 2`; a retailer's own category
  always wins). `groceries categorize [--dry-run]` runs it by hand.
- API: `/api/drill?period=&category=&other_than=&channel=&store=&lines=`.

## v1.3.0 – 2026-11-28

- **Time range**: a *Period* selector in the header with All time, Last week, Last month, Last quarter,
  Last year and Last 2 years. Each is a rolling window ending today. Every tile, chart and table follows
  it, including the price history and the price changes. The choice is kept in the URL (`?range=quarter`)
  and remembered in the browser.
- The last week and the last month are charted per day (days without purchases show as zero); longer
  periods per month.
- Biggest price changes need half the selected period (at most 120 days) between a product's first and
  last purchase, so shorter periods still show changes.
- API: every dashboard endpoint takes `range=all|week|month|quarter|year|2y`.
- `groceries stats --range quarter` prints the summary for a period.

## v1.2.1 – 2026-11-21

- **Automatic reimport after an upgrade.** The first time `groceries serve` or `groceries sync` starts
  with a different version, it re-parses the saved raw responses of the stored purchases once (like
  `groceries reimport`), so parser fixes also reach purchases synced earlier. It logs every changed
  receipt and a summary. The dashboard starts right away (it runs in the background); an error is
  logged, never stops the sync or the dashboard, and is retried on the next start. It does nothing on a
  new database or when the version did not change. The database stores the last version in a new
  `meta` table. Turn it off with `GROCERIES_AUTO_REIMPORT=false` (`0`, `no`, `off` and `disabled` work
  too; an unknown value keeps the default and logs a warning).
- **`groceries accounts remove --purge` also deletes the account's saved raw responses**, so a later
  `groceries reimport` cannot bring the purchases back.
- Fix: `groceries reimport` of a raw file without a stored purchase added it to a second "Main" account
  and failed; it now goes to the retailer's existing account. A purchase that cannot be stored is
  counted as failed and the others carry on.
- **`groceries --version`**. The version is also shown in the dashboard footer and returned by
  `/healthz` (`{"status": "ok", "version": "1.2.1"}`).
- README: new *Upgrading* section for Docker and venv/systemd installs. Fixed the Docker command prefix:
  it is `docker compose exec web groceries …` (the services are `web` and `sync`).

## v1.2.0 – 2026-11-21

- **Fix: Jumbo products got the wrong name.** The product lookup took the first search result for every
  receipt name, and Jumbo's search often puts the same promoted product first, so different products
  ended up with one catalogue title (for example every kind of fruit). A search hit is now only used when
  its title resembles the receipt name; otherwise the receipt name is shown.
- **Fix: Jumbo receipt parsing.** A product name that itself ends in a price-like number (`... 0,01`)
  followed by a `2 X 0,79` line became two lines, one of them named `2 X 0,79`. Tills that send name and
  price as unpadded cells are read correctly. Also handled: receipts without the `OMSCHRIJVING` header,
  names wrapped over several lines, subtotal lines, returned bottle deposit, the plastic levy line, paper
  bags, several payment methods and cash change. Payments are read only from the block after the total.
- A Jumbo receipt whose lines do not add up to its total is logged with its id.
- **`groceries reimport [--provider ah|jumbo] [--account NAME] [--receipt ID] [--dry-run]`**: re-parse
  the raw responses saved by earlier syncs with the current parser and update the database, without
  network or login. Only changed purchases are rewritten; `--dry-run` lists receipts changed, lines added
  or removed and totals before and after. Search matches the new check rejects are cleared and looked up
  again on the next sync.
- **`groceries export-raw --receipt ID [--redact]`**: one purchase's raw response, with ids, card
  details, addresses and store details stripped, to attach to an issue. See *Debugging* in the README.
- Providers implement `parse_raw(channel, raw)`; both Albert Heijn and Jumbo archive their responses as
  `{"summary", "detail"}` under `raw/<retailer>/<channel>/`.
- Dev container (`.devcontainer/`): Python 3.13, ruff, pytest, dashboard on port 8080.

## v1.1.0 – 2026-11-19

- **Several accounts per supermarket**: connect more than one Albert Heijn or Jumbo account (for
  example yours and your partner's) and see them in one household dashboard. Each account has its own
  card under **Connections** (Sync now / Reconnect / Rename / Disconnect) and an **Add another …
  account** button. Connecting a store account that is already connected is refused, and a reconnect
  only accepts the account that slot belongs to.
- **Account filter** on the dashboard: all accounts (default) or one, applied to every tile, chart and
  table. Recent purchases show the account name when there is more than one.
- A receipt that shows up in two accounts (a shared card) is stored once.
- Sync covers every connected account; one failing account no longer stops the others. Alerts name the
  account: `⚠ AH sync failed (Partner)`. Alert titles accept a new `{account}` field; templates without
  it get the account name appended.
- CLI: `groceries login ah --account Partner`, `groceries sync --account Partner`,
  `groceries accounts list|rename|remove` (`remove` asks first and keeps the purchases unless `--purge`).
- Upgrading is automatic: the existing login of each supermarket becomes its account "Main" and keeps all
  purchases (`<retailer>-token.json` moves to `<retailer>-<id>-token.json`).
- Fix: with only one supermarket in the data, the header hid the Connections button too.
- Summary cards: fixed label / value / sub-text rows, so every value lines up across the row at any
  width; shorter labels and sub-texts (full explanation in the tooltip). Savings stamps moved from
  *Products* to the *Bonus savings* card.
- `demo-seed` creates two AH accounts and one Jumbo account, and some savings stamps; README screenshots
  refreshed (light and dark, including Connections).

## v1.0.0

First public release.

- **Albert Heijn**: in-store receipts (the 27 months AH keeps) and online orders, with product
  categories from AH's catalogue (inferred from search for delisted products).
- **Jumbo**: digital store receipts (for purchases where the Jumbo Extra's card was scanned), parsed
  from the printed receipt. Online orders are not imported yet.
- **Connect from the browser**: per-retailer cards with Connect / Sync now / Reconnect / Disconnect, a
  bookmarklet helper and a paste fallback; CLI login for headless setups.
- **Dashboard**: spend per month and category, top products, price history with a regular-price line
  built from your receipts, biggest price changes, personal price index, Bonus savings, stores,
  recent purchases. Light and dark theme, works on phones.
- **History before your receipts** (optional): public shelf prices since November 2022 from the open
  checkjebon dataset, and supermarket payments from a bank CSV export (ING preset, custom mappings).
- **Notifications** (optional): ntfy and/or a webhook when a sync starts failing and when it recovers.
- **Deploy**: Docker / compose, or a Python venv with hardened systemd units and a daily timer.
- **Safety**: logins bound to the browser that started them and confirmed before use, same-origin JSON
  for every state-changing request, DNS-rebinding protection, tokens kept out of logs and browsers,
  private data directory, optional basic auth or authenticating-proxy mode.
- `groceries demo-seed` fills an empty data directory with synthetic purchases for trying it out.
