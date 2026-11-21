"""Re-parse the archived raw JSON with the current parsers and update the database; no network, no login.

Every sync keeps the retailer's response for each purchase under ``<data dir>/raw/<retailer>/<channel>/``.
After a parser fix, ``groceries reimport`` applies the fix to everything already stored. Only purchases
whose parsed result differs are rewritten, so running it twice changes nothing the second time.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import accounts
from .config import Config
from .providers import PROVIDERS, get_provider
from .providers.base import Provider, Purchase
from .storage import file_lock
from .sync import normalize_quantities, store_product, store_purchase

log = logging.getLogger(__name__)

CHANNELS = ("store", "online")


@dataclass
class PurchaseDiff:
    id: str
    total_before: float | None
    total_after: float
    lines_added: list[str] = field(default_factory=list)
    lines_removed: list[str] = field(default_factory=list)
    other: list[str] = field(default_factory=list)  # header, discount and payment changes
    new: bool = False

    @property
    def changed(self) -> bool:
        return self.new or bool(self.lines_added or self.lines_removed or self.other)


@dataclass
class ReimportResult:
    files: int = 0
    unchanged: int = 0
    failed: int = 0
    skipped: int = 0  # raw files without a stored purchase, with stored_only
    store_failed: int = 0  # of `failed`: parsed, but the database refused it (perhaps only for now)
    diffs: list[PurchaseDiff] = field(default_factory=list)
    titles_dropped: list[tuple[str, str, str]] = field(default_factory=list)  # (retailer, receipt name, title)
    products_pruned: int = 0
    total_before: float = 0.0
    total_after: float = 0.0

    @property
    def changed(self) -> list[PurchaseDiff]:
        return [d for d in self.diffs if d.changed]

    def summary(self) -> dict[str, Any]:
        ch = self.changed
        return {
            "raw_files": self.files,
            "receipts_changed": len(ch),
            "receipts_new": sum(d.new for d in ch),
            "receipts_unchanged": self.unchanged,
            "receipts_failed": self.failed,
            "raw_files_skipped": self.skipped,
            "lines_added": sum(len(d.lines_added) for d in ch),
            "lines_removed": sum(len(d.lines_removed) for d in ch),
            "total_before": round(self.total_before, 2),
            "total_after": round(self.total_after, 2),
            "product_titles_dropped": len(self.titles_dropped),
            "products_pruned": self.products_pruned,
        }


def _line_sig(name: str, amount: float, weight: float | None, product_key: str | None) -> str:
    w = f" {weight:g} kg" if weight else ""
    return f"{name} {amount:.2f}{w}" + ("" if product_key else " (no product)")


def _signature(conn: sqlite3.Connection, pid: str) -> dict[str, Any] | None:
    head = conn.execute("SELECT * FROM purchases WHERE id = ?", (pid,)).fetchone()
    if not head:
        return None
    lines = conn.execute(
        "SELECT name, amount, weight, product_key, quantity, unit_price, qty_inferred, is_bonus, deposit "
        "FROM purchase_lines WHERE purchase_id = ? ORDER BY line_no",
        (pid,),
    ).fetchall()
    return {
        "head": dict(head),
        "lines": Counter(_line_sig(r["name"], r["amount"], r["weight"], r["product_key"]) for r in lines),
        # quantities that normalize_quantities() inferred are not what the parser returns: compare raw ones only
        "detail": Counter(
            (
                r["name"],
                round(r["amount"], 2),
                None if r["qty_inferred"] else r["quantity"],
                bool(r["is_bonus"]),
                r["deposit"],
            )
            for r in lines
        ),
        "discounts": Counter(
            (r["name"], round(r["amount"], 2))
            for r in conn.execute("SELECT name, amount FROM discounts WHERE purchase_id = ?", (pid,))
        ),
        "payments": Counter(
            (r["method"], round(r["amount"], 2))
            for r in conn.execute("SELECT method, amount FROM payments WHERE purchase_id = ?", (pid,))
        ),
    }


def diff_purchase(conn: sqlite3.Connection, p: Purchase) -> PurchaseDiff:
    old = _signature(conn, p.id)
    d = PurchaseDiff(p.id, old["head"]["total"] if old else None, p.total)
    if old is None:
        d.new = True
        d.lines_added = [_line_sig(ln.name, ln.amount, ln.weight, ln.product_key) for ln in p.lines]
        return d
    new_lines = Counter(_line_sig(ln.name, ln.amount, ln.weight, ln.product_key) for ln in p.lines)
    d.lines_added = sorted((new_lines - old["lines"]).elements())
    d.lines_removed = sorted((old["lines"] - new_lines).elements())
    inferred = {k[0:2] for k in old["detail"] if k[2] is None}
    new_detail = Counter(
        (
            ln.name,
            round(ln.amount, 2),
            None if (ln.name, round(ln.amount, 2)) in inferred else ln.quantity,
            bool(ln.is_bonus),
            ln.deposit,
        )
        for ln in p.lines
    )
    if not d.lines_added and not d.lines_removed and new_detail != old["detail"]:
        d.other.append("line quantities or Bonus flags")
    h = old["head"]
    for col, value in (
        ("total", p.total),
        ("discount_total", p.discount_total),
        ("ts_utc", p.ts_utc),
        ("store_id", p.store_id),
        ("store_street", p.store_street),
        ("store_city", p.store_city),
        ("store_postcode", p.store_postcode),
        ("status", p.status),
    ):
        if (round(h[col], 2) if isinstance(h[col], float) else h[col]) != (
            round(value, 2) if isinstance(value, float) else value
        ):
            d.other.append(col)
    if Counter((x.name, round(x.amount, 2)) for x in p.discounts) != old["discounts"]:
        d.other.append("discounts")
    if Counter((x.method, round(x.amount, 2)) for x in p.payments) != old["payments"]:
        d.other.append("payments")
    return d


def raw_files(config: Config, retailer: str) -> list[tuple[str, Path]]:
    root = config.raw_dir / retailer
    return [(ch, f) for ch in CHANNELS for f in sorted((root / ch).glob("*.json")) if f.is_file()]


def _owner(conn: sqlite3.Connection, p: Purchase, rel: str) -> tuple[bool, int | None]:
    row = conn.execute("SELECT account_id FROM purchases WHERE id = ?", (p.id,)).fetchone()
    if row:
        return True, row[0]
    row = conn.execute("SELECT account_id FROM purchases WHERE raw_file = ?", (rel,)).fetchone()
    return (True, row[0]) if row else (False, None)


def _matches_receipt(ref: str, p: Purchase, path: Path) -> bool:
    stems = {path.name, path.stem, re.sub(r"\.json$", "", path.stem)}
    return ref in (p.id, p.external_id, re.sub(r"\.json$", "", p.external_id)) or ref in stems


def reimport(
    conn: sqlite3.Connection,
    config: Config,
    retailers: list[str] | None = None,
    account: str | None = None,
    receipt: str | None = None,
    dry_run: bool = False,
    stored_only: bool = False,
) -> ReimportResult:
    """Re-parse the raw files. A raw file without a purchase in the database (one dropped into raw/ by
    hand, or left by a sync that failed to store it) is imported into the retailer's first account,
    unless `stored_only`: then it is skipped, which is what the automatic run after an upgrade does."""
    res = ReimportResult()
    for retailer in retailers or sorted(PROVIDERS):
        with file_lock(config.data_dir / f"sync-{retailer}.lock"):  # not while a sync writes the same rows
            _reimport_retailer(conn, config, res, retailer, account, receipt, dry_run, stored_only)
    return res


def _reimport_retailer(
    conn: sqlite3.Connection,
    config: Config,
    res: ReimportResult,
    retailer: str,
    account: str | None,
    receipt: str | None,
    dry_run: bool,
    stored_only: bool,
) -> None:
    provider = get_provider(retailer, config)
    only = accounts.find(conn, account, retailer)["id"] if account else None
    touched = False
    for channel, path in raw_files(config, retailer):
        try:
            raw = json.loads(path.read_text())
            p = provider.parse_raw(channel, raw)
        except Exception:
            log.warning("%s: cannot parse %s", retailer, path, exc_info=True)
            res.failed += 1
            continue
        if receipt and not _matches_receipt(receipt, p, path):
            continue
        rel = str(path.relative_to(config.data_dir))
        exists, owner = _owner(conn, p, rel)
        if only is not None and exists and owner != only:
            continue
        if stored_only and not exists:
            res.skipped += 1
            continue
        res.files += 1
        d = diff_purchase(conn, p)
        res.diffs.append(d)
        res.total_before += d.total_before or 0
        res.total_after += p.total
        if not d.changed:
            res.unchanged += 1
            continue
        if dry_run:
            continue
        try:
            with conn:  # one transaction per purchase: an error rolls back that purchase only
                # a raw file nobody stored yet (e.g. one shared in an issue) goes to the account asked for
                # or the retailer's first account
                account_id = owner if exists else (only or accounts.default_account(conn, retailer))
                store_purchase(conn, p, config.timezone, rel, account_id)
                for info in provider.known_products(raw):
                    store_product(conn, retailer, info)
        except sqlite3.Error:
            log.warning("%s: cannot store %s", retailer, path, exc_info=True)
            res.diffs.remove(d)  # not changed after all
            res.total_before -= d.total_before or 0
            res.total_after -= p.total
            res.failed += 1
            res.store_failed += 1
            continue
        touched = True
    res.titles_dropped += _check_titles(conn, provider, dry_run)
    if not dry_run:
        res.products_pruned += _prune_products(conn, retailer)
        if touched:
            normalize_quantities(conn, retailer)


def _check_titles(conn: sqlite3.Connection, provider: Provider, dry_run: bool) -> list[tuple[str, str, str]]:
    """Catalogue titles a search matched to a receipt name that the provider no longer accepts. They are
    cleared (and looked up again on the next sync), so the dashboard shows the receipt name instead."""
    bad = [
        (r["product_key"], r["name"], r["title"])
        for r in conn.execute(
            "SELECT product_key, name, title FROM products WHERE retailer = ? AND lookup_state = 'ok' "
            "AND title IS NOT NULL AND webshop_id IS NOT NULL",
            (provider.slug,),
        )
        if r["name"] and not provider.title_matches(r["name"], r["title"])
    ]
    if bad and not dry_run:
        with conn:
            conn.executemany(
                "UPDATE products SET title = NULL, webshop_id = NULL, lookup_state = NULL, enriched_at = NULL "
                "WHERE retailer = ? AND product_key = ?",
                [(provider.slug, key) for key, _n, _t in bad],
            )
    return [(provider.slug, name, title.strip()) for _k, name, title in bad]


def _prune_products(conn: sqlite3.Connection, retailer: str) -> int:
    """Name-keyed products (receipts without product ids) that no line refers to any more."""
    with conn:
        return conn.execute(
            "DELETE FROM products WHERE retailer = ? AND product_key LIKE 'name:%' AND NOT EXISTS "
            "(SELECT 1 FROM purchase_lines l WHERE l.retailer = products.retailer "
            "AND l.product_key = products.product_key)",
            (retailer,),
        ).rowcount


# --- sharing a raw file ------------------------------------------------------------------------
REDACTED = "REDACTED"
_ID_KEYS = {"id", "transactionId", "orderId", "invoiceId", "memberId", "customerId", "loyaltyCardId", "cardNumber"}
_PLACE_KEYS = {
    "address",
    "storeAddress",
    "location",
    "storeInfo",
    "lane",
    "phone",
    "email",
    "emailAddress",
    "orderPayments",
}
_DROP_KEYS = {"barcodeObject", "printCommands", "receiptPoints", "pointBalance"}
_ADDRESS_FIELDS = {"street", "houseNumber", "postalCode", "zipCode", "zipCodeExtra", "city"}
_SENSITIVE_LINE = re.compile(
    r"(card\s*(:|nr|sequence)|kaart\s*:|pas(nummer)?\s*:|terminal|merchant|period|transac|\btrans\b|auth|"
    r"\baid\b|\(A0|\bSTORE \d|\bPOS \d|https?://|www\.|\btel\b|\d{4} ?[A-Z]{2}\b)",
    re.I,
)
_AMOUNT = re.compile(r"-?\d+,\d{2}\b")


def find_raw(config: Config, ref: str) -> tuple[str, str, Path]:
    """The raw file of a purchase, by purchase id, receipt id or file name."""
    for retailer in sorted(PROVIDERS):
        provider = get_provider(retailer, config)
        for channel, path in raw_files(config, retailer):
            if ref in (path.name, path.stem):
                return retailer, channel, path
            try:
                p = provider.parse_raw(channel, json.loads(path.read_text()))
            except Exception:  # noqa: BLE001
                continue
            if _matches_receipt(ref, p, path):
                return retailer, channel, path
    raise LookupError(f"no raw file for {ref!r} under {config.raw_dir}")


def _pseudonym(value: Any) -> str:
    return "redacted-" + hashlib.sha256(str(value).encode()).hexdigest()[:12]


def _mask(text: str) -> str:
    return re.sub(r"[0-9A-Za-z]", "x", text)  # keeps the layout, drops the content


def _redact_text_line(text: str) -> str:
    # digits that are not amounts (times, counts, ids) go; amounts stay so totals still add up
    keep = {m.span() for m in _AMOUNT.finditer(text)}
    out, pos = [], 0
    for m in re.finditer(r"\d+", text):
        if any(a <= m.start() < b for a, b in keep):
            continue
        out.append(text[pos : m.start()] + "0" * len(m.group(0)))
        pos = m.end()
    return "".join(out) + text[pos:]


def _redact_print_doc(doc: Any) -> Any:
    """Jumbo's receipt print document: keep the item block verbatim, mask the header (store address) and
    the card slip, drop barcodes."""
    from .providers.jumbo import _HEADER, _TOTAL

    phase = "header"  # header -> items -> footer

    def text_line(tl: dict[str, Any]) -> dict[str, Any]:
        nonlocal phase
        cells = [t for t in tl.get("texts") or [] if isinstance(t, dict)]
        line = "".join(str(t.get("text") or "") for t in cells)
        if phase == "header" and _HEADER.match(line):
            phase = "items"
            return tl
        if phase == "items":
            if _TOTAL.match(line):
                phase = "footer"
            return tl
        sensitive = phase == "header" or _SENSITIVE_LINE.search(line) or re.search(r"\d{5,}", line)
        mask = _mask if sensitive else _redact_text_line
        return {**tl, "texts": [{**t, "text": mask(str(t.get("text") or ""))} for t in cells]}

    def walk(o: Any) -> Any:
        if isinstance(o, dict):
            if "texts" in o:
                return text_line(o)
            return {k: walk(v) for k, v in o.items() if k not in _DROP_KEYS}
        if isinstance(o, list):
            return [walk(x) for x in o]
        return o

    return walk(doc)


def redact(obj: Any, key: str | None = None) -> Any:
    """Strip ids (transaction, member, card, order), store and home addresses and the store's name from a
    raw purchase. Ids become stable pseudonyms, so the file still re-imports; product ids, names and
    amounts stay, they are what a parser bug report needs."""
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            if k in _DROP_KEYS:
                continue
            if k in _ID_KEYS and not isinstance(v, dict | list) and v is not None:
                out[k] = _pseudonym(v)
            elif k in _PLACE_KEYS or k in _ADDRESS_FIELDS:
                out[k] = _blank(v)
            elif k == "store" and isinstance(v, dict):
                out[k] = {sk: _pseudonym(sv) if sk in ("storeId", "id") else _blank(sv) for sk, sv in v.items()}
            elif k == "store" and v is not None:
                out[k] = _pseudonym(v)
            elif k == "image" and isinstance(v, str):
                try:
                    out[k] = json.dumps(_redact_print_doc(json.loads(v)), ensure_ascii=False)
                except ValueError:
                    out[k] = REDACTED
            elif k == "transaction" and isinstance(v, dict):
                out[k] = {tk: tv if tk == "dateTime" else _pseudonym(tv) for tk, tv in v.items()}
            else:
                out[k] = redact(v, k)
        return out
    if isinstance(obj, list):
        if key == "products":  # AH receipt lines: product ids are public catalogue ids, keep them
            return obj
        return [redact(x, key) for x in obj]
    return obj


def _blank(v: Any) -> Any:
    if isinstance(v, dict):
        return {k: _blank(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_blank(x) for x in v]
    return None if v is None else REDACTED
