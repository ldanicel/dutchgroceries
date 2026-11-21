"""Jumbo provider, using the (unofficial) GraphQL API behind jumbo.com and the Jumbo app.

Login is the app's Auth0 flow (authorization code + PKCE, redirect ``jumbo://callbacks/login``)
with a rotating refresh token. Digital store receipts ("bonnetjes", only for purchases where
the Jumbo Extra's card was scanned) come from ``receiptOverview`` / ``receipt(transactionId)``;
the receipt body is the printed receipt as a JSON print document, so lines are parsed from text
and have no product ids. Online orders come from ``orders``.

Endpoint facts were cross-checked with the MIT-licensed jumbo-mcp project. Parts marked
UNVERIFIED have not been seen with a logged-in account yet and degrade to "skip" on schema
errors instead of failing the sync.

Nothing here is endorsed by Jumbo and the API can change without notice.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import secrets
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from ..config import Config
from ..storage import ensure_private_dir, file_lock, write_json_atomic
from ..tokens import RotatingTokenStore, extract_code, extract_param, jwt_claims, mask_email
from .base import AuthError, Discount, Line, Payment, ProductInfo, Provider, Purchase, PurchaseRef

log = logging.getLogger(__name__)

GRAPHQL = "https://www.jumbo.com/api/graphql"
AUTH_DOMAIN = "https://auth.jumbo.com"
CLIENT_ID = "JxF3wi1b7MOTd9qXutaxqd9uHt4KmG3U"  # public client id of the Jumbo web/app login
AUDIENCE = "https://jumbo.com/web"
REDIRECT_URI = "jumbo://callbacks/login"
SCOPE = "openid profile email offline_access"
CLIENT_VERSION = "master-v39.2.0-web"
BROWSER_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
PAGE_SIZE = 25

RECEIPTS_QUERY = """
query GetReceiptOverview($page: Int, $pageSize: Int) {
  receiptOverview(page: $page, pageSize: $pageSize) {
    totalResults pageSize currentPage
    receipts { transactionId purchaseEndOn receiptSource store { storeId name } pointBalance }
  }
}"""

RECEIPT_QUERY = """
query GetDigitalReceipt($transactionId: String) {
  receipt(transactionId: $transactionId) {
    receiptImage { image type receiptPoints { earned newBalance oldBalance redeemed } }
    store { storeId name location { address { city houseNumber postalCode street } } }
    purchaseEndOn
    receiptSource
  }
}"""

# Shape (OrdersResponse { totalCount orders [...] }) verified against the live API (a response with no
# orders); the per-order fields are UNVERIFIED and degrade to "no online orders" on a schema error.
ORDERS_QUERY = """
query OrdersPageOrders($input: OrdersInput) {
  orders(input: $input) { totalCount orders { id orderDate status totalPrice { amount } fulfilmentType } }
}"""

SEARCH_QUERY = """
query SearchProductsByKeyword($input: ProductSearchInput!) {
  searchProducts(input: $input) { products { title sku } }
}"""

DONE_ORDER_STATES = {"DELIVERED", "COMPLETED", "PICKED_UP", "CLOSED", "INVOICED", "FINISHED"}


# --- receipt document parsing -------------------------------------------------------------------
# The receipt detail carries no item fields: the printed text is the only source of lines. Each printed
# line comes as cells (name, bonus flag, ..., amount); padded on most tills, but not on every one.
_AMOUNT = r"-?\d+,\d{2}"
_MULT = re.compile(rf"^\s*(\d+(?:,\d+)?)\s*(kg)?\s*[xX]\s+(\d+,\d{{2}})(?:\s+P)?\s+({_AMOUNT})\s*$", re.I)
_ITEM = re.compile(rf"^(.*?)(?:\s+P)?\s+({_AMOUNT})\s*$")
_HEADER = re.compile(r"^\s*(OMSCHRIJVING|ARTIKEL(EN)?|DESCRIPTION)\b", re.I)
_TOTAL = re.compile(rf"^\s*(TOTAAL|TE BETALEN)\b(?!\s*(KORTING|BESPAARD|VOORDEEL))\D*({_AMOUNT})", re.I)
_SUBTOTAL = re.compile(r"^\s*SUB\s*-?\s*TOTAAL\b", re.I)
_SEPARATOR = re.compile(r"^[\s=*_-]*$")
_PAYMENT = re.compile(rf"^\s*(.*?\S)\s*:?\s+({_AMOUNT})\s*(EUR)?\s*$", re.I)
_PAYMENT_END = re.compile(r"^\s*(BTW|CUSTOMER|KLANTENTICKET|TOTAAL KORTING|TERMINAL|AANTAL ARTIKELEN)", re.I)
_DEPOSIT_RETURN = re.compile(rf"(retour\s+emballage|emballage|statiegeld\s*retour|statiegeldbon)\D*({_AMOUNT})", re.I)
# bottle deposit, carrier bags and the plastic levy are not products
_NOT_A_PRODUCT = re.compile(
    r"^(STATIEGELD|EMBALLAGE|LEEGGOED|NL INCL\.?\s*HEF|(JUMBO\s+)?(LUS)?DRAAGTAS|(PAPIEREN|PLASTIC)\s+TAS|"
    r"BOODSCHAPPENTAS|TASJE)\b",
    re.I,
)


def _num(text: str) -> float:
    return float(text.replace(".", "").replace(",", "."))


def receipt_text_lines(image: str | dict[str, Any]) -> list[str]:
    """Flatten the receipt print document into its printed lines."""
    data = json.loads(image) if isinstance(image, str) else image
    out: list[str] = []
    docs = data.get("documents") or []
    while docs and "printSections" not in docs[0] and docs[0].get("documents"):
        docs = docs[0]["documents"]
    for doc in docs[:1]:
        for section in doc.get("printSections") or []:
            for obj in section.get("textObjects") or []:
                for tl in obj.get("textLines") or []:
                    text = ""
                    for seg in tl.get("texts") or []:
                        cell = seg.get("text") or ""
                        # unpadded cells ("MELK", "1,19") must not run together into "MELK1,19"
                        if text and cell and not text[-1].isspace() and not cell[0].isspace():
                            text += " "
                        text += cell
                    for part in text.splitlines():  # footer blocks come as one text with newlines
                        if part.strip():
                            out.append(part.rstrip())
    return out


def _item_start(lines: list[str]) -> int | None:
    """Index of the first item line: after the column header, or (no header) after the separator above
    the first priced line."""
    for i, line in enumerate(lines):
        if _HEADER.match(line):
            return i + 1
    sep = None
    for i, line in enumerate(lines):
        if _TOTAL.match(line):
            break
        if _SEPARATOR.match(line) and line.strip():
            sep = i
        elif _ITEM.match(line) and sep is not None:
            return sep + 1
    return None


def parse_receipt_lines(lines: list[str], receipt_id: str | None = None) -> dict[str, Any]:
    """Pull items, discounts, total and payments out of the printed receipt text.

    Item layouts: ``NAME  1,19``; a name line followed by ``2 X 1,25  2,50`` or ``0,512 kg x 3,49  1,79``
    (that name line may itself end in something that looks like a price, e.g. ``... 0,01``); names wrapped
    over several lines; negative lines (discounts, or returned deposit); ``P`` marks a Bonus price."""
    items: list[Line] = []
    discounts: list[Discount] = []
    payments: list[Payment] = []
    total: float | None = None
    deposit_returned = 0.0
    start = _item_start(lines)
    i = start if start is not None else len(lines)
    pending: str | None = None
    while i < len(lines):
        line, stripped = lines[i], lines[i].strip()
        i += 1
        m = _TOTAL.match(line)
        if m:
            total = _num(m.group(3))
            break
        if _SUBTOTAL.match(line) or _SEPARATOR.match(line):
            continue
        nxt = next((ln for ln in lines[i:] if not _SEPARATOR.match(ln)), "")
        m = _MULT.match(line)
        if m:
            qty, is_kg, unit, amount = _num(m.group(1)), bool(m.group(2)), _num(m.group(3)), _num(m.group(4))
            name = pending or (items[-1].name if items else "?")
            items.append(
                Line(
                    name=name,
                    amount=amount,
                    quantity=1 if is_kg else qty,
                    weight=qty if is_kg else None,
                    weight_unit="KG" if is_kg else None,
                    unit_price=unit,
                    is_bonus=bool(re.search(r"\sP\s", line)),
                )
            )
            pending = None
            continue
        m = _ITEM.match(line)
        if m and not _MULT.match(nxt):
            name = f"{pending} {m.group(1).strip()}".strip() if pending else m.group(1).strip()
            amount = _num(m.group(2))
            if amount < 0 and not _NOT_A_PRODUCT.match(name):
                discounts.append(Discount(amount=round(-amount, 2), name=name or "Korting", type="KORTING"))
            else:  # a negative deposit line is bottles handed in: keep it, it is part of the total
                items.append(Line(name=name or "?", amount=amount, unit_price=amount, is_bonus=" P " in line))
            pending = None
        else:  # a name without a price: the price (or the "2 X ..." line) follows
            pending = f"{pending} {stripped}" if pending else stripped
    # payments: the block right after the total, up to the next separator or the card slip / VAT table
    while i < len(lines):
        line = lines[i]
        i += 1
        if _SEPARATOR.match(line) or _PAYMENT_END.match(line):
            break
        m = _DEPOSIT_RETURN.search(line)
        if m:
            deposit_returned += _num(m.group(2))
            continue
        m = _PAYMENT.match(line)
        if m:
            payments.append(Payment(method=re.sub(r"\s+", " ", m.group(1)).upper(), amount=_num(m.group(2))))
    if deposit_returned:
        payments.append(Payment(method="EMBALLAGE", amount=round(deposit_returned, 2)))
    lines_total = round(sum(ln.amount for ln in items) - sum(d.amount for d in discounts), 2)
    if total is None:
        total = lines_total
    elif abs(lines_total - total) > 0.011:
        log.warning(
            "Jumbo receipt %s: lines add up to %.2f but the receipt total is %.2f; "
            "please report it with `groceries export-raw --redact`",
            receipt_id or "?",
            lines_total,
            total,
        )
    for ln in items:
        ln.product_key = None if _NOT_A_PRODUCT.match(ln.name) else name_key(ln.name)
    return {"lines": items, "discounts": discounts, "payments": payments, "total": total}


def name_key(name: str) -> str:
    """Store receipts carry no product ids; the printed name is the most stable key available."""
    return "name:" + re.sub(r"\s+", " ", name.strip().upper())


_STOP_WORDS = {"JUMBO", "JU", "JUM", "DE", "HET", "MET", "EN", "VAN", "IN", "OP", "STUKS", "ST", "STUK"}


def _words(text: str) -> list[str]:
    return [w for w in re.split(r"[^0-9A-ZÀ-Ü]+", text.upper()) if w]


def _word_matches(word: str, title_words: list[str]) -> bool:
    """Receipt names are cut short and glue words together ('KRENTENBOL', 'VOLK', 'PNDKS',
    'MINIPAPRIKA'): a prefix, a compound part, or an abbreviation (same first letter, letters in
    order) of a title word counts."""
    for t in title_words:
        if t.startswith(word) or (len(t) >= 3 and word.startswith(t)):
            return True
        if (len(t) >= 5 and t in word) or (len(word) >= 5 and word in t):
            return True
        if len(word) >= 4 and word[0] == t[0] and len(word) * 2 >= len(t):
            it = iter(t)
            if all(c in it for c in word):
                return True
    return False


def _name_words(name: str) -> list[str]:
    return [w for w in _words(name) if len(w) >= 3 and not any(c.isdigit() for c in w) and w not in _STOP_WORDS]


def title_score(name: str, title: str) -> float:
    """Share of the receipt name's meaningful words found in a catalogue title (0..1)."""
    words = _name_words(name)
    if not words:
        return 0.0
    title_words = _words(title)
    return sum(_word_matches(w, title_words) for w in words) / len(words)


def title_matches(name: str, title: str) -> bool:
    """Whether a catalogue title plausibly is the product on the receipt: most of its words match, or
    half of them including the first (usually the brand or the product itself)."""
    words = _name_words(name)
    score = title_score(name, title)
    return score >= 0.6 or (score >= 0.5 and _word_matches(words[0], _words(title)))


def best_hit(name: str, hits: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The search hit whose title resembles the receipt name most, or None when none does."""
    scored = [
        (title_score(name, h["title"]), -n, h)
        for n, h in enumerate(hits)
        if (h.get("title") or "").strip() and title_matches(name, h["title"])
    ]
    return max(scored, key=lambda x: x[:2])[2] if scored else None


def parse_receipt(summary: dict[str, Any], receipt: dict[str, Any]) -> Purchase:
    image = (receipt.get("receiptImage") or {}).get("image") or "{}"
    parsed = parse_receipt_lines(receipt_text_lines(image), summary.get("transactionId"))
    store = receipt.get("store") or summary.get("store") or {}
    addr = ((store.get("location") or {}).get("address")) or {}
    street = " ".join(p for p in (addr.get("street"), addr.get("houseNumber")) if p) or store.get("name")
    return Purchase(
        retailer="jumbo",
        channel="store",
        external_id=summary["transactionId"],
        ts_utc=_iso_utc(receipt.get("purchaseEndOn") or summary.get("purchaseEndOn")),
        total=parsed["total"],
        lines=parsed["lines"],
        discounts=parsed["discounts"],
        payments=parsed["payments"],
        store_id=str(store["storeId"]) if store.get("storeId") else None,
        store_street=street,
        store_city=addr.get("city"),
        store_postcode=addr.get("postalCode"),
    )


def parse_order(summary: dict[str, Any]) -> Purchase:
    total = (summary.get("totalPrice") or {}).get("amount")
    return Purchase(
        retailer="jumbo",
        channel="online",
        external_id=str(summary["id"]),
        ts_utc=_iso_utc(summary.get("orderDate")),
        total=float(total or 0),
        status=summary.get("status"),
        store_street=summary.get("fulfilmentType"),
    )


def _iso_utc(value: Any) -> str:
    """Jumbo sends local Dutch time without an offset ('2025-01-15 12:00:00'); store it as UTC."""
    if isinstance(value, int | float):  # epoch millis
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(value / 1000 if value > 1e11 else value))
    if not value:
        return "1970-01-01T00:00:00Z"
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("Europe/Amsterdam"))
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def receipt_id_candidates(raw: str) -> list[str]:
    """receiptOverview returns a storage key like '<prefix>-<uuid>.json'; the detail query
    resolves on one of its forms (whole key, key without .json, the uuid, the prefix)."""
    out = [raw.strip()]
    stem = re.sub(r"\.json$", "", out[0], flags=re.I)
    out.append(stem)
    m = re.match(r"^(.*)-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$", stem, re.I)
    if m:
        out += [m.group(2), m.group(1)]
    return list(dict.fromkeys(c for c in out if c))


# --- auth ---------------------------------------------------------------------------------------
def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def authorize_url(challenge: str, state: str, nonce: str, response_mode: str = "query") -> str:
    """response_mode=form_post makes Auth0 return an HTML form holding the code instead of
    redirecting to jumbo://, so a page script (the Connect helper) can read it."""
    params = httpx.QueryParams(
        client_id=CLIENT_ID,
        redirect_uri=REDIRECT_URI,
        audience=AUDIENCE,
        scope=SCOPE,
        response_type="code",
        response_mode=response_mode,
        code_challenge=challenge,
        code_challenge_method="S256",
        state=state,
        nonce=nonce,
        prompt="login",
    )
    return f"{AUTH_DOMAIN}/authorize?{params}"


class JumboTokenStore(RotatingTokenStore):
    slug = "jumbo"
    label = "Jumbo"

    def __init__(self, secrets_dir, client: httpx.Client, account: int | str | None = None) -> None:  # type: ignore[no-untyped-def]
        super().__init__(secrets_dir, account)
        self.client = client
        self.pending = secrets_dir / ("jumbo-login.json" if account is None else f"jumbo-{account}-login.json")

    def _token(self, body: dict[str, Any]) -> dict[str, Any]:
        r = self.client.post(f"{AUTH_DOMAIN}/oauth/token", json=body, headers={"User-Agent": BROWSER_UA})
        if r.status_code in (400, 401, 403):
            err = r.json().get("error") if "json" in r.headers.get("content-type", "") else r.status_code
            raise AuthError(f"Jumbo login server said {err}; log in again with `groceries login jumbo`")
        r.raise_for_status()
        return r.json()

    def _refresh(self, tok: dict[str, Any]) -> dict[str, Any]:
        return self._token(
            {
                "grant_type": "refresh_token",
                "client_id": CLIENT_ID,
                "refresh_token": tok["refresh_token"],
                "audience": AUDIENCE,
                "scope": SCOPE,
            }
        )

    def start_login(self) -> str:
        verifier, challenge = pkce_pair()
        state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
        ensure_private_dir(self.dir)
        write_json_atomic(self.pending, {"verifier": verifier, "state": state, "created": int(time.time())})
        return authorize_url(challenge, state, nonce)

    def exchange(self, code: str, verifier: str) -> dict[str, Any]:
        """Code -> tokens. Returns the raw response (with id_token); only the pair is persisted."""
        raw = self._token(
            {
                "grant_type": "authorization_code",
                "client_id": CLIENT_ID,
                "code": code,
                "code_verifier": verifier,
                "redirect_uri": REDIRECT_URI,
                "audience": AUDIENCE,
                "scope": SCOPE,
            }
        )
        with file_lock(self.lock):
            self.save(raw)
        return raw

    def finish_login(self, pasted: str) -> dict[str, Any]:
        try:
            pending = json.loads(self.pending.read_text())
        except FileNotFoundError:
            raise AuthError("no login in progress; run `groceries login jumbo` again") from None
        state = extract_param(pasted, "state")
        if state is not None and state != pending["state"]:
            raise AuthError("the pasted URL belongs to a different login attempt (state mismatch)")
        tok = self.exchange(extract_code(pasted), pending["verifier"])
        self.pending.unlink(missing_ok=True)
        return tok


class Jumbo(Provider):
    slug = "jumbo"
    display_name = "Jumbo"

    def __init__(self, config: Config, client: httpx.Client | None = None, account: int | str | None = None) -> None:
        super().__init__(config, account)
        self.client = client or httpx.Client(timeout=30)
        self.tokens = JumboTokenStore(config.secrets_dir, self.client, account)
        self._last_call = 0.0
        self._search_ok = True

    def _pace(self) -> None:
        wait = self.config.request_delay - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()

    def graphql(
        self,
        query: str,
        variables: dict[str, Any] | None = None,
        client_name: str = "JUMBO_WEB-orders",
        strict: bool = True,
    ) -> dict[str, Any]:
        refresh = refreshed = False  # only an auth failure justifies rotating the refresh token
        for attempt in range(4):
            self._pace()
            headers = {
                "User-Agent": BROWSER_UA,
                "Content-Type": "application/json",
                "Accept": "application/json",
                "apollographql-client-name": client_name,
                "apollographql-client-version": CLIENT_VERSION,
                "x-source": client_name,
                "Authorization": f"Bearer {self.tokens.access_token(force_refresh=refresh)}",
            }
            refresh = False
            try:
                r = self.client.post(GRAPHQL, json={"query": query, "variables": variables or {}}, headers=headers)
            except httpx.TransportError as e:
                log.warning("Jumbo GraphQL: %s (attempt %d)", e, attempt + 1)
                time.sleep(2**attempt)
                continue
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(5 * 2**attempt)
                continue
            body = r.json() if "json" in r.headers.get("content-type", "") else {}
            errors = body.get("errors") or []
            unauth = r.status_code == 401 or any(
                (e.get("extensions") or {}).get("code") == "UNAUTHENTICATED"
                or "Customer ID" in (e.get("message") or "")
                for e in errors
            )
            if unauth and not refreshed:
                refresh = refreshed = True
                continue  # retry once with a forced refresh
            if unauth:
                raise AuthError("Jumbo rejected a freshly refreshed token; run `groceries login jumbo`")
            if r.status_code >= 400 or (errors and not body.get("data")):
                msg = errors[0].get("message") if errors else f"HTTP {r.status_code}"
                if strict:
                    raise RuntimeError(f"Jumbo GraphQL error: {msg}")
                log.warning("Jumbo GraphQL (non-fatal): %s", msg)
                return {}
            return body.get("data") or {}
        raise RuntimeError("Jumbo GraphQL kept failing")

    def check_auth(self) -> None:
        self.tokens.access_token()

    def list_purchases(self) -> Iterator[PurchaseRef]:
        page = 0
        while True:
            ov = self.graphql(RECEIPTS_QUERY, {"page": page, "pageSize": PAGE_SIZE}).get("receiptOverview") or {}
            receipts = ov.get("receipts") or []
            for r in receipts:
                yield PurchaseRef(channel="store", external_id=r["transactionId"], summary=r)
            total = ov.get("totalResults") or 0
            page += 1
            if not receipts or page * PAGE_SIZE >= total:
                break
        resp = self.graphql(ORDERS_QUERY, {"input": {"first": 100}}, strict=False).get("orders") or {}
        orders = resp.get("orders") if isinstance(resp, dict) else resp
        for o in orders or []:
            yield PurchaseRef(
                channel="online",
                external_id=str(o["id"]),
                final=str(o.get("status") or "").upper() in DONE_ORDER_STATES,
                summary=o,
            )

    def fetch_purchase(self, ref: PurchaseRef) -> tuple[dict[str, Any], Purchase]:
        if ref.channel == "online":
            raw: dict[str, Any] = {"summary": ref.summary}
            return raw, self.parse_raw("online", raw)
        for candidate in receipt_id_candidates(ref.external_id):
            # an id form the server does not like may come back as an error: try the next one
            receipt = self.graphql(RECEIPT_QUERY, {"transactionId": candidate}, strict=False).get("receipt")
            if receipt:
                raw = {"summary": ref.summary, "detail": receipt}
                return raw, self.parse_raw("store", raw)
        raise RuntimeError(f"Jumbo receipt {ref.external_id} not found under any id form")

    def parse_raw(self, channel: str, raw: dict[str, Any]) -> Purchase:
        if channel == "online":
            return parse_order(raw["summary"])
        return parse_receipt(raw["summary"], raw["detail"])

    def lookup_products(self, items: list[tuple[str, str]]) -> Iterator[ProductInfo]:
        """Map receipt names to catalogue titles via search. Category is not exposed by the
        search query known so far, so Jumbo products stay uncategorized for now (UNVERIFIED).

        The search always returns something, often a promoted product first, so a hit is only used
        when its title resembles the receipt name (see ``title_matches``)."""
        for key, name in items:
            if not self._search_ok:
                return
            data = self.graphql(
                SEARCH_QUERY,
                {"input": {"searchType": "KEYWORD", "searchTerms": name[:60]}},
                client_name="JUMBO_WEB-loyalty",
                strict=False,
            )
            if not data:
                self._search_ok = False  # schema changed or unavailable; stop for this run
                return
            hits = (data.get("searchProducts") or {}).get("products") or []
            best = best_hit(name, hits)
            if best:
                yield ProductInfo(
                    product_key=key, lookup_state="ok", webshop_id=best.get("sku"), title=best["title"].strip()
                )
            else:
                yield ProductInfo(product_key=key, lookup_state="missing")

    def title_matches(self, name: str, title: str) -> bool:
        return title_matches(name, title)

    def begin_login(self, state: str) -> tuple[str, dict[str, Any]]:
        verifier, challenge = pkce_pair()
        # The popup uses the normal redirect: it fails on jumbo:// and leaves the window on auth.jumbo.com.
        # (A top-level form_post page trips Chrome's "form is not secure" interstitial.) The Connect helper
        # then fetches its own form_post authorization with the login session and its own PKCE pair.
        return authorize_url(challenge, state, secrets.token_urlsafe(24)), {"verifier": verifier}

    def complete_login(self, pending: dict[str, Any], code: str) -> str | None:
        raw = self.tokens.exchange(code, pending["verifier"])
        email = jwt_claims(raw.get("id_token")).get("email")
        email = email or jwt_claims(raw.get("access_token")).get("https://jumbo.com/username")
        return mask_email(email)

    def identity(self) -> str | None:
        return jwt_claims(self.tokens.load().get("access_token")).get("sub")

    def interactive_login(self) -> None:
        url = self.tokens.start_login()
        print("1. Open the developer tools of a desktop browser (Network tab, 'Preserve log' on),")
        print("   then open this URL and log in with your Jumbo account:\n")
        print(f"   {url}\n")
        print("2. After login the browser tries to open jumbo://callbacks/login?code=...&state=...")
        print("   and fails. Copy that URL from the Network tab (or the address bar).")
        pasted = input("\nPaste the URL: ")
        if not extract_code(pasted):
            raise SystemExit("no code given")
        self.tokens.finish_login(pasted)
        print(f"Saved tokens to {self.tokens.path} (mode 600).")

    def import_refresh_token(self, refresh_token: str) -> None:
        with file_lock(self.tokens.lock):
            self.tokens.save(self.tokens._refresh({"refresh_token": refresh_token.strip()}))
