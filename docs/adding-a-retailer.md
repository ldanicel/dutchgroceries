# Adding a retailer

A retailer is a *provider*: one module in `src/groceries/providers/` that implements the `Provider`
interface from `providers/base.py`. The database, sync engine, alerts and dashboard need no changes.

## 1. Implement the provider

```python
# src/groceries/providers/example.py
from collections.abc import Iterator
from typing import Any

from .base import AuthError, Line, ProductInfo, Provider, Purchase, PurchaseRef


class ExampleMarket(Provider):
    slug = "example"          # short id, stored in every row
    display_name = "Example"  # shown in the UI and in alerts

    def check_auth(self) -> None:
        """Raise AuthError if there is no usable login (the UI then asks to reconnect)."""

    def list_purchases(self) -> Iterator[PurchaseRef]:
        """Yield every purchase the retailer still has, newest first.
        Set final=False for purchases that can still change (e.g. an order not yet delivered)."""

    def fetch_purchase(self, ref: PurchaseRef) -> tuple[dict[str, Any], Purchase]:
        """Return (raw JSON to archive, normalized Purchase): fetch the raw JSON, then parse_raw() it."""

    def parse_raw(self, channel: str, raw: dict[str, Any]) -> Purchase:
        """Normalize a purchase from the archived raw JSON alone, without network.
        `groceries reimport` calls it to apply parser fixes to purchases already stored."""

    def lookup_products(self, items: list[tuple[str, str]]) -> Iterator[ProductInfo]:
        """Catalogue details for (product key, receipt name) pairs: title, brand, category."""
```

Optional hooks:

- `known_products(raw)`: product details that come with a purchase for free (online orders often have them).
- `title_matches(name, title)`: when `lookup_products` maps receipt names to catalogue titles by search,
  say whether a stored title still belongs to the name; `reimport` clears the ones that do not.
- `begin_login(state)` / `complete_login(pending, code)`: the browser connect flow. Return the authorize
  URL plus private data to keep (such as a PKCE verifier), then exchange the code, store the tokens and
  return a masked account label.
- `interactive_login()`: the terminal login used by `groceries login <slug>`.

Guidelines:

- **Normalize amounts** to euros as floats. `Line.amount` is the line total before receipt-level discounts;
  `Discount.amount` is positive (money saved). The sum of lines plus deposits minus discounts should equal
  `Purchase.total`; tests are the place to prove it.
- **Times**: `Purchase.ts_utc` in ISO 8601 UTC. Convert local times with an explicit timezone.
- **Product keys** must be stable over time; prefer the retailer's id over the printed name.
- **Tokens**: subclass `tokens.RotatingTokenStore` and implement `_refresh`. It persists every new token
  atomically before use and shares a lock with disconnects.
- **Be polite**: pace requests with `config.request_delay`, retry 429/5xx with backoff, and only force a
  token refresh after an authentication failure.
- **Fail soft** for optional data: log a warning and skip, rather than failing the whole sync.

## 2. Register it

Add a factory to `PROVIDERS` in `src/groceries/providers/__init__.py`:

```python
def _example(config: Config) -> Provider:
    from .example import ExampleMarket

    return ExampleMarket(config)


PROVIDERS = {"ah": _ah, "jumbo": _jumbo, "example": _example}
```

The Connections panel shows a card for every registered provider. Add a display name in
`connect.DISPLAY` and the user-facing connect instructions in the `HELP` object in `static/index.html`.
If the login ends in a custom-scheme redirect, teach `static/helper.js` how to find the code on the
retailer's page.

## 3. Test it

Add synthetic fixtures under `tests/fixtures/` (never real receipts: they contain store visits, payment
details and loyalty ids) and tests for parsing, totals and the login flow with `httpx.MockTransport`.
See `tests/test_ah_parse.py` and `tests/test_jumbo.py` for examples.
