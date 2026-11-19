"""Albert Heijn provider, using the (unofficial) API of the Appie mobile app.

In-store receipts come from the app's GraphQL endpoint (``posReceiptsPage`` /
``posReceiptDetails``; the old ``/mobile-services/v1/receipts`` REST list is gone).
Online orders come from ``orderFulfillments`` plus the order detail REST endpoint.
Receipt lines carry a POS product id; ``productConvertId`` maps it to the webshop
id, and ``product(id:)`` gives the catalogue category.

Nothing here is endorsed by Albert Heijn and the API can change without notice.
"""

from __future__ import annotations

import logging
import time
from collections import Counter
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from ..config import Config
from ..storage import file_lock
from ..tokens import RotatingTokenStore, extract_code, mask_email
from .base import AuthError, Discount, Line, Payment, ProductInfo, Provider, Purchase, PurchaseRef

log = logging.getLogger(__name__)

API = "https://api.ah.nl"
AUTHORIZE_URL = (
    "https://login.ah.nl/secure/oauth/authorize?client_id=appie&redirect_uri=appie://login-exit&response_type=code"
)
PAGE_SIZE = 50
CONVERT_BATCH = 50
PRODUCT_BATCH = 25

RECEIPTS_QUERY = """
query FetchPosReceipts($offset: Int!, $limit: Int!) {
  posReceiptsPage(pagination: {offset: $offset, limit: $limit}) {
    pagination { offset limit totalElements }
    posReceipts {
      id dateTime totalAmount { amount }
      storeAddress { street houseNumber postalCode city countryCode }
    }
  }
}"""

RECEIPT_QUERY = """
query FetchReceipt($id: String!) {
  posReceiptDetails(id: $id) {
    id storeInfo storeType
    products {
      id quantity name externalIds
      weight { amount unit }
      price { amount } amount { amount } deposit { amount }
      indicators { name discount percentage }
    }
    subtotalProducts { quantity amount { amount } }
    discounts { type name amount { amount } }
    discountTotal { amount }
    discountPersonal { description amount { amount } }
    subtotalDiscount { amount }
    stamps { quantity amount { amount } }
    total { amount }
    payments { method amount { amount } }
    change { amount }
    promotions { quantity name }
    transaction { store lane id dateTime }
    address { street houseNumber postalCode city countryCode }
    vat { levels { percentage salesAmount { amount } } total { salesAmount { amount } amount { amount } } }
  }
}"""

ORDERS_QUERY = """
query FetchFulfillments {
  orderFulfillments {
    page { total hasNextPage }
    result {
      orderId closingDateTime statusCode statusDescription shoppingType transactionCompleted
      totalPrice {
        beforeDiscount { amount } afterDiscount { amount } discount { amount } totalPrice { amount }
      }
      delivery { method status }
    }
  }
}"""

PRODUCT_FIELDS = "id title brand category salesUnitSize"
SEARCH_QUERY = "query Search($q: String!) { productSearch(input: {query: $q}) { products { id title category } } }"
STAMPS_KEY = "stamps"
STAMPS_TITLE = "Koopzegels"
STAMPS_PRODUCT = ProductInfo(product_key=STAMPS_KEY, lookup_state="ok", title=STAMPS_TITLE, category="Koopzegels")


def _money(obj: dict[str, Any] | None) -> float | None:
    if not obj or obj.get("amount") is None:
        return None
    return float(obj["amount"])


def _local_to_utc(value: str | None) -> str:
    """The receipt's own transaction time is local Dutch time without an offset."""
    if not value:
        raise ValueError("receipt has no date")
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("Europe/Amsterdam"))
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _street(addr: dict[str, Any] | None) -> str | None:
    if not addr or not addr.get("street"):
        return None
    return " ".join(p for p in (addr.get("street"), addr.get("houseNumber")) if p)


def parse_receipt(summary: dict[str, Any], detail: dict[str, Any]) -> Purchase:
    """Normalize a posReceiptsPage list item plus its posReceiptDetails."""
    addr = detail.get("address") or summary.get("storeAddress") or {}
    tx = detail.get("transaction") or {}
    store_info = detail.get("storeInfo") or []
    lines: list[Line] = []
    for p in detail.get("products") or []:
        amount = _money(p.get("amount"))
        if amount is None:
            continue
        qty = float(p.get("quantity") or 1)
        weight = p.get("weight") or {}
        w_amount = weight.get("amount")
        unit_price = _money(p.get("price"))
        if w_amount:
            unit_price = round(amount / float(w_amount), 4)
        elif unit_price is None and qty:
            unit_price = round(amount / qty, 4)
        pid = p.get("id")
        indicators = p.get("indicators") or []
        lines.append(
            Line(
                name=(p.get("name") or "").strip() or "?",
                amount=amount,
                quantity=qty,
                product_key=str(pid) if str(pid or "").isdigit() and int(pid) > 0 else None,
                unit_price=unit_price,
                weight=float(w_amount) if w_amount else None,
                weight_unit=weight.get("unit") if w_amount else None,
                deposit=_money(p.get("deposit")),
                is_bonus=any(i.get("name") == "bonusDiscount" for i in indicators),
            )
        )
    stamps = _money((detail.get("stamps") or {}).get("amount"))
    if stamps:
        # savings stamps (koopzegels) bought at the till are part of the total but not a product line
        lines.append(Line(name=STAMPS_TITLE, amount=stamps, product_key=STAMPS_KEY, unit_price=stamps))
    discounts = [
        Discount(amount=round(-(_money(d.get("amount")) or 0), 2), name=d.get("name"), type=d.get("type"))
        for d in detail.get("discounts") or []
        if _money(d.get("amount"))
    ]
    # discountPersonal (Bonus Box) is already one of the entries in discounts
    payments = [
        Payment(method=p.get("method"), amount=_money(p.get("amount")) or 0) for p in detail.get("payments") or []
    ]
    total = _money(detail.get("total"))
    if total is None:
        total = _money(summary.get("totalAmount")) or 0.0
    store_id = tx.get("store") or (store_info[0] if store_info else None)
    return Purchase(
        retailer="ah",
        channel="store",
        external_id=summary["id"],
        ts_utc=summary.get("dateTime") or _local_to_utc(tx.get("dateTime")),
        total=total,
        lines=lines,
        discounts=discounts,
        payments=payments,
        store_id=str(store_id) if store_id else None,
        store_street=_street(addr),
        store_city=addr.get("city"),
        store_postcode=addr.get("postalCode"),
    )


def _order_ts(fulfillment: dict[str, Any], detail: dict[str, Any]) -> str:
    period = detail.get("deliveryTimePeriod") or {}
    if period.get("startTimeUtc"):
        return period["startTimeUtc"]
    if detail.get("deliveryDate"):
        return detail["deliveryDate"] + "T12:00:00Z"
    return fulfillment.get("closingDateTime")


def _order_products(detail: dict[str, Any]) -> Iterator[dict[str, Any]]:
    for group in detail.get("groupedProductsInTaxonomy") or []:
        yield from group.get("orderedProducts") or []


def parse_order(fulfillment: dict[str, Any], detail: dict[str, Any]) -> Purchase:
    """Normalize an orderFulfillments item plus its details-grouped-by-taxonomy."""
    lines: list[Line] = []
    for op in _order_products(detail):
        prod = op.get("product") or {}
        qty = float(op.get("allocatedQuantity", op.get("quantity")) or 0)
        if qty <= 0:
            continue  # not delivered
        unit = prod.get("priceBeforeBonus")
        if unit is None:
            unit = prod.get("currentPrice") or 0
        key = prod.get("hqId") or prod.get("webshopId")
        lines.append(
            Line(
                name=prod.get("title") or "?",
                amount=round(float(unit) * qty, 2),
                quantity=qty,
                product_key=str(key) if key else None,
                unit_price=float(unit),
                is_bonus=bool(prod.get("isBonus")),
            )
        )
    price = fulfillment.get("totalPrice") or {}
    saved = _money(price.get("discount")) or 0
    total = _money(price.get("totalPrice"))
    if total is None:
        total = _money(price.get("afterDiscount")) or 0.0
    return Purchase(
        retailer="ah",
        channel="online",
        external_id=str(fulfillment["orderId"]),
        ts_utc=_order_ts(fulfillment, detail),
        total=total,
        lines=lines,
        discounts=[Discount(amount=abs(saved), name="Bonus", type="BONUS")] if saved else [],
        status=detail.get("orderState") or fulfillment.get("statusDescription"),
        store_street=(fulfillment.get("delivery") or {}).get("method"),
    )


def order_products(detail: dict[str, Any]) -> Iterator[ProductInfo]:
    for op in _order_products(detail):
        prod = op.get("product") or {}
        key = prod.get("hqId") or prod.get("webshopId")
        if not key:
            continue
        yield ProductInfo(
            product_key=str(key),
            lookup_state="ok",
            webshop_id=str(prod["webshopId"]) if prod.get("webshopId") else None,
            title=prod.get("title"),
            brand=prod.get("brand"),
            category=prod.get("mainCategory"),
            subcategory=prod.get("subCategory"),
            unit_size=prod.get("salesUnitSize"),
        )


def parse_product(key: str, webshop_id: int, product: dict[str, Any] | None) -> ProductInfo:
    if not product:
        return ProductInfo(product_key=key, lookup_state="missing", webshop_id=str(webshop_id))
    main, _, sub = (product.get("category") or "").partition("/")
    return ProductInfo(
        product_key=key,
        lookup_state="ok",
        webshop_id=str(product.get("id") or webshop_id),
        title=product.get("title"),
        brand=product.get("brand"),
        category=main or None,
        subcategory=sub or None,
        unit_size=product.get("salesUnitSize"),
        raw=product,
    )


class TokenStore(RotatingTokenStore):
    slug = "ah"
    label = "AH"

    def __init__(self, secrets_dir: Path, client: httpx.Client) -> None:
        super().__init__(secrets_dir)
        self.client = client

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        r = self.client.post(API + path, json=body)
        if r.status_code in (400, 401, 403):
            raise AuthError(f"AH token endpoint said {r.status_code}; log in again with `groceries login ah`")
        r.raise_for_status()
        return r.json()

    def _refresh(self, tok: dict[str, Any]) -> dict[str, Any]:
        return self._post(
            "/mobile-auth/v1/auth/token/refresh", {"clientId": "appie", "refreshToken": tok["refresh_token"]}
        )

    def exchange_code(self, code: str) -> dict[str, Any]:
        with file_lock(self.lock):
            return self.save(self._post("/mobile-auth/v1/auth/token", {"clientId": "appie", "code": code}))


class AlbertHeijn(Provider):
    slug = "ah"
    display_name = "AH"

    def __init__(self, config: Config, client: httpx.Client | None = None) -> None:
        super().__init__(config)
        self.client = client or httpx.Client(
            timeout=30,
            headers={"User-Agent": config.user_agent, "Content-Type": "application/json"},
        )
        self.tokens = TokenStore(config.secrets_dir, self.client)
        self._last_call = 0.0

    # -- HTTP -----------------------------------------------------------------------------
    def _pace(self) -> None:
        wait = self.config.request_delay - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        refresh = refreshed = False  # only an auth failure justifies rotating the refresh token
        for attempt in range(4):
            self._pace()
            headers = {"Authorization": f"Bearer {self.tokens.access_token(force_refresh=refresh)}"}
            refresh = False
            try:
                r = self.client.request(method, API + path, json=body, headers=headers)
            except httpx.TransportError as e:
                log.warning("AH %s %s: %s (attempt %d)", method, path, e, attempt + 1)
                time.sleep(2**attempt)
                continue
            if r.status_code == 401 and not refreshed:
                refresh = refreshed = True
                continue  # retry once with a forced refresh
            if r.status_code == 401:
                raise AuthError("AH API rejected a freshly refreshed token")
            if r.status_code == 429 or r.status_code >= 500:
                log.warning("AH %s %s: HTTP %d (attempt %d)", method, path, r.status_code, attempt + 1)
                time.sleep(5 * 2**attempt)
                continue
            r.raise_for_status()
            return r.json()
        raise RuntimeError(f"AH {method} {path} kept failing")

    def graphql(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        resp = self._request("POST", "/graphql", {"query": query, "variables": variables or {}})
        if resp.get("errors") and not resp.get("data"):
            raise RuntimeError(f"AH GraphQL error: {resp['errors'][0].get('message')}")
        return resp.get("data") or {}

    # -- Provider -------------------------------------------------------------------------
    def check_auth(self) -> None:
        self.tokens.access_token()

    def list_purchases(self) -> Iterator[PurchaseRef]:
        offset = 0
        while True:
            page = self.graphql(RECEIPTS_QUERY, {"offset": offset, "limit": PAGE_SIZE})["posReceiptsPage"]
            receipts = page.get("posReceipts") or []
            for r in receipts:
                yield PurchaseRef(channel="store", external_id=r["id"], summary=r)
            total = (page.get("pagination") or {}).get("totalElements") or 0
            offset += len(receipts)
            if not receipts or offset >= total:
                break
        for f in (self.graphql(ORDERS_QUERY)["orderFulfillments"] or {}).get("result") or []:
            yield PurchaseRef(
                channel="online",
                external_id=str(f["orderId"]),
                final=bool(f.get("transactionCompleted")),
                summary=f,
            )

    def fetch_purchase(self, ref: PurchaseRef) -> tuple[dict[str, Any], Purchase]:
        if ref.channel == "store":
            detail = self.graphql(RECEIPT_QUERY, {"id": ref.external_id})["posReceiptDetails"]
            return {"summary": ref.summary, "detail": detail}, parse_receipt(ref.summary, detail)
        detail = self._request("GET", f"/mobile-services/order/v1/{int(ref.external_id)}/details-grouped-by-taxonomy")
        return {"summary": ref.summary, "detail": detail}, parse_order(ref.summary, detail)

    def known_products(self, raw: dict[str, Any]) -> Iterator[ProductInfo]:
        detail = raw.get("detail") or {}
        if "groupedProductsInTaxonomy" in detail:
            yield from order_products(detail)
        elif _money((detail.get("stamps") or {}).get("amount")):
            yield STAMPS_PRODUCT

    def _search_category(self, text: str, brand: str | None = None) -> tuple[str | None, str | None]:
        """Best-effort category for a product the catalogue no longer describes.

        Tries the full title, then without the brand, then its last words, and takes the
        most common category among the top hits of the first query that finds anything.
        """
        words = [w for w in text.replace("/", " ").split() if len(w) > 1]
        if brand:
            unbranded = [w for w in words if w.lower() not in brand.lower().split()]
        else:
            unbranded = words[1:] if len(words) > 2 else words
        tried: set[str] = set()
        for cand in (words, unbranded, unbranded[-2:], unbranded[-1:]):
            q = " ".join(cand)[:80]
            if not q or q in tried:
                continue
            tried.add(q)
            hits = (self.graphql(SEARCH_QUERY, {"q": q}).get("productSearch") or {}).get("products") or []
            cats = [h["category"] for h in hits[:5] if h.get("category")]
            if cats:
                mains = Counter(c.partition("/")[0] for c in cats)
                main = mains.most_common(1)[0][0]
                sub = next(c.partition("/")[2] for c in cats if c.startswith(main + "/") or c == main)
                return main, sub or None
        return None, None

    def lookup_products(self, items: list[tuple[str, str]]) -> Iterator[ProductInfo]:
        names = dict(items)
        ids = [int(k) for k, _ in items if k.isdigit()]
        webshop: dict[int, int] = {}
        found: list[ProductInfo] = []
        for i in range(0, len(ids), CONVERT_BATCH):
            batch = ids[i : i + CONVERT_BATCH]
            # ids are ints from our own database, so inlining them as aliases is safe
            data = self.graphql(
                "query Convert {" + " ".join(f"p{n}: productConvertId(sourceId: {n})" for n in batch) + "}"
            )
            for n in batch:
                v = data.get(f"p{n}")
                if v and v > 0:
                    webshop[n] = v
                else:
                    found.append(ProductInfo(product_key=str(n), lookup_state="unmapped"))
        pairs = list(webshop.items())
        for i in range(0, len(pairs), PRODUCT_BATCH):
            batch = pairs[i : i + PRODUCT_BATCH]
            query = (
                "query Products {"
                + " ".join(f"p{pos}: product(id: {ws}) {{ {PRODUCT_FIELDS} }}" for pos, ws in batch)
                + "}"
            )
            resp = self._request("POST", "/graphql", {"query": query, "variables": {}})
            data = resp.get("data") or {}
            found.extend(parse_product(str(pos), ws, data.get(f"p{pos}")) for pos, ws in batch)
        for info in found:
            if not info.category:
                info.category, info.subcategory = self._search_category(
                    info.title or names.get(info.product_key, ""), info.brand
                )
                info.category_inferred = bool(info.category)
            yield info

    def begin_login(self, state: str) -> tuple[str, dict[str, Any]]:
        return AUTHORIZE_URL, {}  # AH's flow carries no state; the cookie-bound pending login covers CSRF

    def complete_login(self, pending: dict[str, Any], code: str) -> str | None:
        self.tokens.exchange_code(code)
        try:
            return mask_email((self.graphql("{ member { emailAddress } }").get("member") or {}).get("emailAddress"))
        except Exception:  # noqa: BLE001  (the label is cosmetic)
            return None

    def interactive_login(self) -> None:
        print("1. Open this URL in a desktop browser and log in with your AH account:\n")
        print(f"   {AUTHORIZE_URL}\n")
        print("2. Before submitting, open the developer tools (Network tab). After login the browser")
        print("   tries to open appie://login-exit?code=...; copy that URL (or just the code).")
        pasted = input("\nPaste the URL or code: ")
        code = extract_code(pasted)
        if not code:
            raise SystemExit("no code given")
        self.tokens.exchange_code(code)
        print(f"Saved tokens to {self.tokens.path} (mode 600).")

    def import_refresh_token(self, refresh_token: str) -> None:
        """Bootstrap from a refresh token obtained elsewhere; it is rotated immediately."""
        with file_lock(self.tokens.lock):
            new = self.tokens._post(
                "/mobile-auth/v1/auth/token/refresh", {"clientId": "appie", "refreshToken": refresh_token.strip()}
            )
            self.tokens.save(new)
