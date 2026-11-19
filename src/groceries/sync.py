"""Pull purchases from a provider into the database and the raw JSON archive."""

from __future__ import annotations

import hashlib
import logging
import re
import sqlite3
import statistics
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from .config import Config
from .db import connect
from .notify import Notifier
from .providers import get_provider
from .providers.base import AuthError, ProductInfo, Provider, Purchase
from .storage import file_lock, write_json_atomic

log = logging.getLogger(__name__)


@dataclass
class SyncResult:
    retailer: str
    listed: int = 0
    fetched: int = 0
    failed: int = 0
    products_enriched: int = 0


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def local_date(ts_utc: str, tz: str) -> str:
    dt = datetime.fromisoformat(ts_utc.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(ZoneInfo(tz)).date().isoformat()


def store_purchase(conn: sqlite3.Connection, p: Purchase, tz: str, raw_file: str | None) -> None:
    conn.execute("DELETE FROM purchases WHERE id = ?", (p.id,))  # cascades to lines/discounts/payments
    conn.execute(
        """INSERT INTO purchases (id, retailer, channel, external_id, ts_utc, local_date, store_id, store_street,
               store_city, store_postcode, total, discount_total, item_count, status, raw_file)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            p.id,
            p.retailer,
            p.channel,
            p.external_id,
            p.ts_utc,
            local_date(p.ts_utc, tz),
            p.store_id,
            p.store_street,
            p.store_city,
            p.store_postcode,
            p.total,
            p.discount_total,
            int(sum(ln.quantity if not ln.weight else 1 for ln in p.lines if ln.amount > 0)),
            p.status,
            raw_file,
        ),
    )
    conn.executemany(
        """INSERT INTO purchase_lines (purchase_id, line_no, retailer, product_key, name, quantity, weight,
               weight_unit, unit_price, amount, deposit, is_bonus) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        [
            (
                p.id,
                i,
                p.retailer,
                ln.product_key,
                ln.name,
                ln.quantity,
                ln.weight,
                ln.weight_unit,
                ln.unit_price,
                ln.amount,
                ln.deposit,
                int(ln.is_bonus),
            )
            for i, ln in enumerate(p.lines)
        ],
    )
    conn.executemany(
        "INSERT INTO discounts (purchase_id, idx, type, name, amount) VALUES (?,?,?,?,?)",
        [(p.id, i, d.type, d.name, d.amount) for i, d in enumerate(p.discounts)],
    )
    conn.executemany(
        "INSERT INTO payments (purchase_id, idx, method, amount) VALUES (?,?,?,?)",
        [(p.id, i, pm.method, pm.amount) for i, pm in enumerate(p.payments)],
    )
    for ln in p.lines:
        if ln.product_key:
            conn.execute(
                """INSERT INTO products (retailer, product_key, name) VALUES (?,?,?)
                   ON CONFLICT (retailer, product_key) DO UPDATE SET name = excluded.name""",
                (p.retailer, ln.product_key, ln.name),
            )


def store_product(conn: sqlite3.Connection, retailer: str, info: ProductInfo) -> None:
    conn.execute(
        """INSERT INTO products (retailer, product_key, webshop_id, title, brand, category, subcategory, unit_size,
               category_inferred, lookup_state, enriched_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT (retailer, product_key) DO UPDATE SET
               webshop_id = COALESCE(excluded.webshop_id, webshop_id),
               title = COALESCE(excluded.title, title), brand = COALESCE(excluded.brand, brand),
               category = COALESCE(excluded.category, category),
               subcategory = COALESCE(excluded.subcategory, subcategory),
               unit_size = COALESCE(excluded.unit_size, unit_size),
               category_inferred = CASE WHEN excluded.category IS NULL THEN category_inferred
                                        ELSE excluded.category_inferred END,
               lookup_state = CASE WHEN lookup_state = 'ok' AND excluded.lookup_state != 'ok'
                                   THEN lookup_state ELSE excluded.lookup_state END,
               enriched_at = excluded.enriched_at""",
        (
            retailer,
            info.product_key,
            info.webshop_id,
            info.title,
            info.brand,
            info.category,
            info.subcategory,
            info.unit_size,
            int(info.category_inferred),
            info.lookup_state,
            _now(),
        ),
    )


def _sync(
    provider: Provider, conn: sqlite3.Connection, config: Config, full: bool, refresh_products: bool
) -> SyncResult:
    res = SyncResult(provider.slug)
    provider.check_auth()
    known = {
        (r["channel"], r["external_id"]): r["status"]
        for r in conn.execute("SELECT channel, external_id, status FROM purchases WHERE retailer = ?", (provider.slug,))
    }
    raw_root = config.raw_dir / provider.slug
    for ref in provider.list_purchases():
        res.listed += 1
        if not full and ref.final and (ref.channel, ref.external_id) in known:
            continue
        # one odd receipt must not block every older one behind it: log it, count it, carry on
        try:
            raw, purchase = provider.fetch_purchase(ref)
            raw_path = raw_root / safe_name(ref.channel) / f"{safe_name(ref.external_id)}.json"
            write_json_atomic(raw_path, raw, 0o600)
            with conn:
                store_purchase(conn, purchase, config.timezone, str(raw_path.relative_to(config.data_dir)))
                for info in provider.known_products(raw):
                    store_product(conn, provider.slug, info)
        except AuthError:
            raise
        except Exception:
            log.warning("%s: skipping %s purchase %s", provider.slug, ref.channel, ref.external_id, exc_info=True)
            res.failed += 1
            continue
        res.fetched += 1
        if res.fetched % 25 == 0:
            log.info("%s: fetched %d purchases so far", provider.slug, res.fetched)

    fixed = normalize_quantities(conn, provider.slug)
    if fixed:
        log.info("%s: inferred quantity for %d receipt lines", provider.slug, fixed)

    # new products, plus uncategorized ones once a month (the catalogue or search may know them by now)
    where = (
        ""
        if refresh_products
        else ("AND (p.lookup_state IS NULL OR (p.category IS NULL AND p.enriched_at < datetime('now', '-30 days')))")
    )
    items = [
        (r[0], r[1])
        for r in conn.execute(
            f"""SELECT l.product_key, MAX(l.name) FROM purchase_lines l
            LEFT JOIN products p ON p.retailer = l.retailer AND p.product_key = l.product_key
            WHERE l.retailer = ? AND l.product_key IS NOT NULL {where} GROUP BY l.product_key""",
            (provider.slug,),
        )
    ]
    if items:
        log.info("%s: looking up %d products", provider.slug, len(items))
        prod_dir = raw_root / "products"
        for info in provider.lookup_products(items):
            if info.raw:
                write_json_atomic(prod_dir / f"{safe_name(info.product_key)}.json", info.raw, 0o600)
            with conn:
                store_product(conn, provider.slug, info)
            res.products_enriched += 1
    return res


def safe_name(value: str) -> str:
    """A file name from a retailer-supplied id: never a path, never empty, stable and unique."""
    clean = re.sub(r"[^A-Za-z0-9._-]", "_", str(value)).strip(".")[:120]
    if clean != str(value) or not clean:
        clean = f"{clean or 'id'}-{hashlib.sha256(str(value).encode()).hexdigest()[:10]}"
    return clean


def run_sync(config: Config, retailer: str, full: bool = False, refresh_products: bool = False) -> SyncResult:
    with file_lock(config.data_dir / f"sync-{retailer}.lock"):  # the timer and the web UI may overlap
        return _run_sync(config, retailer, full, refresh_products)


def _run_sync(config: Config, retailer: str, full: bool, refresh_products: bool) -> SyncResult:
    conn = connect(config.db_path)
    notifier = Notifier(config)
    provider = get_provider(retailer, config)
    with conn:
        # a run still marked 'running' was killed (we hold the per-retailer lock now)
        conn.execute(
            "UPDATE sync_runs SET status = 'aborted', finished_at = ? WHERE retailer = ? AND status = 'running'",
            (_now(), retailer),
        )
        run_id = conn.execute(
            "INSERT INTO sync_runs (retailer, started_at, status) VALUES (?, ?, 'running')", (retailer, _now())
        ).lastrowid
    try:
        res = _sync(provider, conn, config, full, refresh_products)
    except Exception as e:
        kind = "login needed" if isinstance(e, AuthError) else type(e).__name__
        msg = f"{kind}: {e}"
        with conn:
            conn.execute(
                "UPDATE sync_runs SET finished_at = ?, status = 'error', message = ? WHERE id = ?",
                (_now(), msg[:2000], run_id),
            )
        notifier.failed(provider.display_name, msg)
        raise
    msg = f"{res.fetched} new/updated purchases, {res.products_enriched} products looked up ({res.listed} listed)"
    if res.failed:
        msg += f"; {res.failed} skipped after errors (see the log)"
    with conn:
        conn.execute(
            "UPDATE sync_runs SET finished_at = ?, status = ?, new_purchases = ?, message = ? WHERE id = ?",
            (_now(), "partial" if res.failed else "ok", res.fetched, msg, run_id),
        )
    notifier.succeeded(provider.display_name, msg)
    log.info("%s: %s", retailer, msg)
    return res


def normalize_quantities(conn: sqlite3.Connection, retailer: str, window_days: int = 90) -> int:
    """Repair lines where the retailer reports quantity 1 for several items.

    AH's receipt API sometimes returns e.g. 3 mangos as one line with quantity 1 and the amount of
    three (often on Bonus lines). When a line's amount is a clean multiple (x2, x3, ...) of the
    product's usual unit price around that date, store the inferred quantity and per-item price.
    The usual price comes from lines with an explicit quantity > 1 nearby, else from the most
    common single-item amount nearby.
    """
    rows = conn.execute(
        """SELECT l.purchase_id, l.line_no, l.product_key, l.quantity, l.amount, l.qty_inferred, p.local_date
           FROM purchase_lines l JOIN purchases p ON p.id = l.purchase_id
           WHERE l.retailer = ? AND l.product_key IS NOT NULL AND l.product_key != 'stamps'
             AND l.weight IS NULL AND l.amount > 0
           ORDER BY l.product_key, p.local_date""",
        (retailer,),
    ).fetchall()
    by_product: dict[str, list[sqlite3.Row]] = {}
    for r in rows:
        by_product.setdefault(r["product_key"], []).append(r)
    fixed = 0
    with conn:
        for lines in by_product.values():
            days = [date.fromisoformat(r["local_date"]) for r in lines]
            all_explicit = [x["amount"] / x["quantity"] for x in lines if x["quantity"] > 1 and not x["qty_inferred"]]
            for i, r in enumerate(lines):
                if r["quantity"] != 1 or r["qty_inferred"]:
                    continue
                near = [j for j in range(len(lines)) if j != i and abs((days[j] - days[i]).days) <= window_days]
                explicit = [
                    lines[j]["amount"] / lines[j]["quantity"]
                    for j in near
                    if lines[j]["quantity"] > 1 and not lines[j]["qty_inferred"]
                ]
                if explicit or (all_explicit and len(all_explicit) >= 3):
                    ref = statistics.median(explicit or all_explicit)
                else:
                    singles = Counter(round(lines[j]["amount"], 2) for j in near if lines[j]["quantity"] == 1)
                    common = [amt for amt, n in singles.items() if n >= 2]
                    if not common:
                        continue
                    ref = min(common)
                k = r["amount"] / ref
                if ref > 0 and 1.9 <= k <= 10.1 and abs(k - round(k)) <= 0.02:
                    q = round(k)
                    conn.execute(
                        "UPDATE purchase_lines SET quantity = ?, unit_price = ?, qty_inferred = 1 "
                        "WHERE purchase_id = ? AND line_no = ?",
                        (q, round(r["amount"] / q, 4), r["purchase_id"], r["line_no"]),
                    )
                    fixed += 1
    return fixed
