# Changelog

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
