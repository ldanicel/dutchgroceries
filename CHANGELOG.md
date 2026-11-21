# Changelog

## Unreleased

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
