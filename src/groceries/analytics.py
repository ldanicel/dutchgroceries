"""Read-only queries behind the dashboard.

"Spend" means what the groceries cost: receipt total minus savings stamps bought at the till
(those come back later as a KOOPZEGELS payment, so counting both would double count).
Category and product figures use line amounts before receipt-level Bonus discounts, because
AH reports those discounts per receipt, not per line.
"""

from __future__ import annotations

import re
import sqlite3
import statistics
from collections import defaultdict
from datetime import date, timedelta
from typing import Any

UNKNOWN = "Onbekend"
NON_PRODUCT_KEYS = ("stamps",)

# time-range presets: rolling windows that end today (name -> label, calendar months back or days back)
RANGES: dict[str, tuple[str, int, int]] = {
    "week": ("Last week", 0, 7),
    "month": ("Last month", 1, 0),
    "quarter": ("Last quarter", 3, 0),
    "year": ("Last year", 12, 0),
    "2y": ("Last 2 years", 24, 0),
}


def range_start(name: str | None, today: date | None = None) -> date | None:
    """First day of a rolling time-range preset ending `today` (inclusive), None for all time.

    "month" is the day after the same day one calendar month back (clamped to that month's last
    day): on 28 November it starts 29 October, on 31 March on 1 March. "week" is the last 7 days
    including today.
    """
    if not name or name == "all":
        return None
    if name not in RANGES:
        raise ValueError(f"unknown range {name!r}; one of: all, {', '.join(RANGES)}")
    today = today or date.today()
    _label, months, days = RANGES[name]
    if days:
        return today - timedelta(days=days - 1)
    y, m = divmod(today.year * 12 + today.month - 1 - months, 12)
    m += 1
    last = (date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)).day
    return date(y, m, min(today.day, last)) + timedelta(days=1)


_SPEND = """(p.total - COALESCE((SELECT SUM(amount) FROM purchase_lines s
              WHERE s.purchase_id = p.id AND s.product_key IN ('stamps')), 0))"""


class Analytics:
    def __init__(
        self,
        conn: sqlite3.Connection,
        retailer: str | None = None,
        account: int | None = None,
        since: date | str | None = None,
        bucket: str = "month",
    ) -> None:
        """`account`: only purchases synced from that store account (bank imports have no account, so
        they are left out then). `since`: only purchases on or after that date (see `range_start`).
        `bucket`: "month" or "day", the period the time series (months, categories over time, price
        index) are grouped by; their rows keep the key `month` either way."""
        self.conn = conn
        self.retailer = retailer
        self.account = account
        self.since = since.isoformat() if isinstance(since, date) else since
        if bucket not in ("month", "day"):
            raise ValueError(f"unknown bucket {bucket!r}")
        self.bucket = bucket
        self._n = 10 if bucket == "day" else 7  # length of the date prefix that names a period

    def _filter(self, alias: str = "p") -> tuple[str, list[Any]]:
        sql, args = "", []
        if self.retailer:
            sql, args = f" AND {alias}.retailer = ?", [self.retailer]
        if self.account is not None:
            if alias == "p":
                sql += " AND p.account_id = ?"
            elif alias == "b":
                sql += " AND 0"  # bank payments belong to no store account
            else:
                sql += f" AND {alias}.purchase_id IN (SELECT id FROM purchases WHERE account_id = ?)"
            if alias != "b":
                args.append(self.account)
        if self.since:
            if alias == "p":
                sql += " AND p.local_date >= ?"
            elif alias == "b":
                sql += " AND b.date >= ?"
            else:
                sql += f" AND {alias}.purchase_id IN (SELECT id FROM purchases WHERE local_date >= ?)"
            args.append(self.since)
        return sql, args

    def _rows(self, sql: str, params: list[Any] | tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute(sql, params)]

    def overview(self) -> dict[str, Any]:
        f, a = self._filter()
        row = self._rows(
            f"""SELECT COUNT(*) AS purchases, MIN(local_date) AS first, MAX(local_date) AS last,
                       ROUND(SUM({_SPEND}), 2) AS spend, ROUND(SUM(discount_total), 2) AS saved,
                       ROUND(AVG({_SPEND}), 2) AS avg_basket,
                       SUM(channel = 'store') AS store_visits, SUM(channel = 'online') AS online_orders
                FROM purchases p WHERE 1=1 {f}""",
            a,
        )[0]
        months = self._receipt_months(7)  # averages only over months with receipts, not bank-only months
        row["months"] = len(months)
        row["avg_month"] = round(sum(m["spend"] for m in months) / len(months), 2) if months else 0
        full = [m for m in months if row["first"] and m["month"] > row["first"][:7] and m["month"] < row["last"][:7]]
        row["avg_full_month"] = round(sum(m["spend"] for m in full) / len(full), 2) if full else row["avg_month"]
        row["stamps_bought"] = (
            self._scalar(
                f"SELECT ROUND(SUM(l.amount), 2) FROM purchase_lines l JOIN purchases p ON p.id = l.purchase_id "
                f"WHERE l.product_key = 'stamps' {f}",
                a,
            )
            or 0
        )
        row["products"] = self._scalar(
            f"SELECT COUNT(DISTINCT l.product_key) FROM purchase_lines l JOIN purchases p ON p.id = l.purchase_id "
            f"WHERE l.product_key IS NOT NULL AND l.product_key != 'stamps' {f}",
            a,
        )
        row["since"] = self.since
        row["last_sync"] = self._rows(
            "SELECT retailer, started_at, finished_at, status, message FROM sync_runs ORDER BY id DESC LIMIT 1"
        )
        return row

    def _scalar(self, sql: str, params: list[Any]) -> Any:
        r = self.conn.execute(sql, params).fetchone()
        return r[0] if r else None

    def months(self) -> list[dict[str, Any]]:
        """Receipt-based months, plus bank-statement spend for months before receipts begin."""
        rows = {m["month"]: {**m, "bank": 0.0} for m in self._receipt_months()}
        for b in self.bank_months():
            m = rows.setdefault(
                b["month"],
                {
                    "month": b["month"],
                    "spend": 0.0,
                    "store": 0.0,
                    "online": 0.0,
                    "saved": 0.0,
                    "baskets": 0,
                    "avg_basket": None,
                    "avg_items": None,
                    "bank": 0.0,
                },
            )
            m["bank"] = b["bank"]
        for k in self._empty_days(rows):
            rows[k] = {"month": k, "spend": 0.0, "store": 0.0, "online": 0.0, "saved": 0.0, "baskets": 0,
                       "avg_basket": None, "avg_items": None, "bank": 0.0}  # fmt: skip
        return [rows[k] for k in sorted(rows)]

    def _empty_days(self, have: Any) -> list[str]:
        """With day buckets, the days of the period without purchases, so charts show them as zero."""
        if self.bucket != "day" or not self.since:
            return []
        start, today = date.fromisoformat(self.since), date.today()
        days = (start + timedelta(days=i) for i in range((today - start).days + 1))
        return [d.isoformat() for d in days if d.isoformat() not in have]

    def bank_months(self) -> list[dict[str, Any]]:
        """Bank payments per month, only for dates before that retailer's first receipt or order."""
        f, a = self._filter("b")
        return self._rows(
            f"""SELECT substr(b.date, 1, {self._n}) AS month, ROUND(SUM(b.amount), 2) AS bank, COUNT(*) AS payments
                FROM bank_payments b
                WHERE b.date < COALESCE((SELECT MIN(local_date) FROM purchases p WHERE p.retailer = b.retailer),
                                        '9999') {f}
                GROUP BY month ORDER BY month""",
            a,
        )

    def _receipt_months(self, n: int | None = None) -> list[dict[str, Any]]:
        f, a = self._filter()
        return self._rows(
            f"""SELECT substr(local_date, 1, {n or self._n}) AS month,
                       ROUND(SUM({_SPEND}), 2) AS spend,
                       ROUND(SUM(CASE WHEN channel = 'store' THEN {_SPEND} ELSE 0 END), 2) AS store,
                       ROUND(SUM(CASE WHEN channel = 'online' THEN {_SPEND} ELSE 0 END), 2) AS online,
                       ROUND(SUM(discount_total), 2) AS saved,
                       COUNT(*) AS baskets,
                       ROUND(AVG({_SPEND}), 2) AS avg_basket,
                       ROUND(AVG(item_count), 1) AS avg_items
                FROM purchases p WHERE 1=1 {f} GROUP BY month ORDER BY month""",
            a,
        )

    def categories(self) -> list[dict[str, Any]]:
        f, a = self._filter()
        return self._rows(
            f"""SELECT COALESCE(pr.category, '{UNKNOWN}') AS category, ROUND(SUM(l.amount), 2) AS spend,
                       COUNT(*) AS lines, COUNT(DISTINCT l.product_key) AS products
                FROM purchase_lines l JOIN purchases p ON p.id = l.purchase_id
                LEFT JOIN products pr ON pr.retailer = l.retailer AND pr.product_key = l.product_key
                WHERE l.amount > 0 AND COALESCE(l.product_key, '') NOT IN ('stamps') {f}
                GROUP BY 1 ORDER BY spend DESC""",
            a,
        )

    def category_months(self, top: int = 8) -> dict[str, Any]:
        cats = [c["category"] for c in self.categories()[:top]]
        f, a = self._filter()
        rows = self._rows(
            f"""SELECT substr(p.local_date, 1, {self._n}) AS month, COALESCE(pr.category, '{UNKNOWN}') AS category,
                       ROUND(SUM(l.amount), 2) AS spend
                FROM purchase_lines l JOIN purchases p ON p.id = l.purchase_id
                LEFT JOIN products pr ON pr.retailer = l.retailer AND pr.product_key = l.product_key
                WHERE l.amount > 0 AND COALESCE(l.product_key, '') NOT IN ('stamps') {f}
                GROUP BY 1, 2""",
            a,
        )
        have = {r["month"] for r in rows}
        months = sorted(have | set(self._empty_days(have)))
        series: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        for r in rows:
            series[r["category"] if r["category"] in cats else "Overig"][r["month"]] += r["spend"]
        names = [*cats, "Overig"] if "Overig" in series else cats
        return {
            "months": months,
            "series": [{"name": n, "data": [round(series[n].get(m, 0), 2) for m in months]} for n in names],
        }

    def products(self, order: str = "spend", limit: int = 25) -> list[dict[str, Any]]:
        f, a = self._filter()
        col = {"spend": "spend", "count": "times"}.get(order, "spend")
        return self._rows(
            f"""SELECT l.retailer, l.product_key, COALESCE(pr.title, MAX(l.name)) AS name, pr.brand,
                       COALESCE(pr.category, '{UNKNOWN}') AS category, pr.unit_size,
                       ROUND(SUM(l.amount), 2) AS spend, COUNT(DISTINCT l.purchase_id) AS times,
                       ROUND(SUM(CASE WHEN l.weight IS NULL THEN l.quantity ELSE 0 END), 1) AS units,
                       MIN(p.local_date) AS first, MAX(p.local_date) AS last
                FROM purchase_lines l JOIN purchases p ON p.id = l.purchase_id
                LEFT JOIN products pr ON pr.retailer = l.retailer AND pr.product_key = l.product_key
                WHERE l.product_key IS NOT NULL AND l.product_key NOT IN ('stamps') AND l.amount > 0 {f}
                GROUP BY l.retailer, l.product_key ORDER BY {col} DESC, spend DESC LIMIT ?""",
            [*a, limit],
        )

    def price_history(self, retailer: str, product_key: str) -> dict[str, Any]:
        """Everything the price chart needs, in one unit per product.

        Packaged products are shown per pack, loose produce per kg. A weighed pack's receipt amount
        already is the pack price; a per-pack price is converted to per kg with the pack size when
        the product is shown per kg. `regular` is the month-by-month regular price: the median of
        the non-Bonus receipt prices where there are any (the receipts are the ground truth), else
        the cleaned public shelf price. Every receipt line counts: AH deducts Bonus discounts from the
        receipt total, so a line always carries the regular (pre-discount) price. Packs priced by their
        weight say nothing about the regular price, so for those only the public data is used.
        """
        info = self._rows("SELECT * FROM products WHERE retailer = ? AND product_key = ?", (retailer, product_key))
        product = info[0] if info else {"product_key": product_key}
        rows = self._rows(
            f"""SELECT p.local_date AS date, l.unit_price, l.weight, l.weight_unit, l.quantity, l.amount, l.is_bonus,
                      l.qty_inferred, p.channel, p.retailer, p.store_street, p.store_city
               FROM purchase_lines l JOIN purchases p ON p.id = l.purchase_id
               WHERE l.retailer = ? AND l.product_key = ? AND l.unit_price > 0
               {"AND p.account_id = ?" if self.account is not None else ""}
               {"AND p.local_date >= ?" if self.since else ""} ORDER BY p.ts_utc""",
            (
                retailer,
                product_key,
                *([self.account] if self.account is not None else []),
                *([self.since] if self.since else []),
            ),
        )
        size = (product.get("unit_size") or "").strip().lower()
        pack_kg = _pack_kg(size)
        loose = bool(re.search(r"kilogram|per kg|^1 kg$", size)) or (
            not size and rows and all(r["weight"] for r in rows)
        )
        unit = "kg" if loose else "pack"
        points = []
        for r in rows:
            pt = dict(r)
            if unit == "pack":
                pt["price"] = round(r["amount"] / (r["quantity"] or 1), 2)
                if r["weight"]:
                    pt["converted"] = f"pack of {r['weight']:g} kg at {r['unit_price']:.2f}/kg"
            elif r["weight"]:
                pt["price"] = round(r["unit_price"], 2)
            elif pack_kg:
                pt["price"] = round(r["unit_price"] / pack_kg, 2)
                pt["converted"] = f"{r['unit_price']:.2f} per pack of {pack_kg:g} kg"
            else:
                continue  # per-pack price of loose produce without a known weight: not comparable
            points.append(pt)
        raw = self.conn.execute(
            "SELECT substr(date, 1, 7) AS month, price FROM market_prices WHERE retailer = ? AND product_key = ? "
            "AND date >= ? ORDER BY date",
            (retailer, product_key, (self.since or "")[:7]),
        ).fetchall()
        paid: dict[str, list[float]] = defaultdict(list)
        for pt in points:
            paid[pt["date"][:7]].append(pt["price"])
        same_day: dict[str, set[float]] = defaultdict(set)
        for pt in points:
            same_day[pt["date"]].add(pt["price"])
        variable = unit == "pack" and (any(len(v) > 1 for v in same_day.values()) or size.startswith("ca."))
        market, note = clean_shelf_prices(raw, {} if variable else paid) if unit == "pack" else ([], None)
        receipts = {} if variable else {m: round(statistics.median(v), 2) for m, v in paid.items()}
        regular: dict[str, dict[str, Any]] = {}
        for mk in market:
            m = mk["date"][:7]
            if mk["price"] is None or m in receipts:
                continue
            # between receipt months the receipts are the truth: skip a public price that disagrees with them
            near = [p for r, p in receipts.items() if abs(_month_index(r) - _month_index(m)) <= 2]
            if near and any(abs(mk["price"] / p - 1) > 0.03 for p in near):
                continue
            regular[m] = {"price": mk["price"], "source": "public"}
        for m, price in receipts.items():
            regular[m] = {"price": price, "source": "receipts"}
        return {
            "product": product,
            "unit": unit,
            "points": points,
            "regular": [{"date": m + "-15", **v} for m, v in sorted(regular.items())],
            "market_note": note,
            "variable_price": variable,
            "skipped": len(rows) - len(points),
            "since": self.since,
        }

    def market_changes(self, limit: int = 25, by: str = "spend") -> list[dict[str, Any]]:
        """Long-term shelf price change from public data, for the products you buy most (in the
        selected period, if any; the price change always spans all the public data).

        Compares the median listed price in the first and in the latest month that has data,
        which smooths out single-day promotions.
        """
        f, a = self._filter("l")
        top = self._rows(
            f"""SELECT l.retailer, l.product_key, COALESCE(pr.title, MAX(l.name)) AS name, SUM(l.amount) AS spend,
                       COUNT(DISTINCT l.purchase_id) AS times
                FROM purchase_lines l
                JOIN (SELECT DISTINCT retailer, product_key FROM market_prices) mk
                  ON mk.retailer = l.retailer AND mk.product_key = l.product_key
                LEFT JOIN products pr ON pr.retailer = l.retailer AND pr.product_key = l.product_key
                WHERE 1=1
                {f} GROUP BY l.retailer, l.product_key ORDER BY {"times" if by == "count" else "spend"} DESC LIMIT ?""",
            [*a, limit],
        )
        out = []
        for t in top:
            rows = self.conn.execute(
                "SELECT substr(date, 1, 7) AS month, price FROM market_prices WHERE retailer = ? AND product_key = ?"
                " ORDER BY date",
                (t["retailer"], t["product_key"]),
            ).fetchall()
            series, _note = clean_shelf_prices(rows, {})
            valid = [x for x in series if x["price"] is not None]
            if not valid:
                continue
            first, last = valid[0]["date"][:7], valid[-1]["date"][:7]
            old, new = valid[0]["price"], valid[-1]["price"]
            out.append(
                {
                    **t,
                    "spend": round(t["spend"], 2),
                    "first_month": first,
                    "last_month": last,
                    "old_price": round(old, 2),
                    "new_price": round(new, 2),
                    "pct": round((new / old - 1) * 100, 1) if old else None,
                }
            )
        return out

    def price_changes(self, min_points: int = 4, min_days: int = 120, limit: int = 25) -> list[dict[str, Any]]:
        """Compare the median shelf price of a product's first and last ~quarter of observations.

        With a period (`since`): only products bought in that period, and their price then (the
        median of what you paid in the period) against their first prices over all history.
        """
        since, self.since = self.since, None
        try:
            f, a = self._filter()
        finally:
            self.since = since
        rows = self.conn.execute(
            f"""SELECT l.retailer, l.product_key, COALESCE(pr.title, l.name) AS name, l.weight_unit,
                       p.local_date, l.unit_price
                FROM purchase_lines l JOIN purchases p ON p.id = l.purchase_id
                LEFT JOIN products pr ON pr.retailer = l.retailer AND pr.product_key = l.product_key
                WHERE l.product_key IS NOT NULL AND l.product_key NOT IN ('stamps') AND l.unit_price > 0 {f}
                ORDER BY p.ts_utc""",
            a,
        ).fetchall()
        by: dict[tuple[str, str], list[sqlite3.Row]] = defaultdict(list)
        for r in rows:
            by[(r["retailer"], r["product_key"])].append(r)
        out = []
        for (retailer, key), obs in by.items():
            if len(obs) < min_points:
                continue
            d0, d1 = date.fromisoformat(obs[0]["local_date"]), date.fromisoformat(obs[-1]["local_date"])
            if (d1 - d0).days < min_days:
                continue
            k = max(1, len(obs) // 4)
            recent = obs[-k:]
            if since:
                recent = [o for o in obs if o["local_date"] >= since]
                if not recent:
                    continue  # not bought in this period
            old = statistics.median(o["unit_price"] for o in obs[:k])
            new = statistics.median(o["unit_price"] for o in recent)
            if old <= 0:
                continue
            out.append(
                {
                    "retailer": retailer,
                    "product_key": key,
                    "name": obs[-1]["name"],
                    "per": (obs[-1]["weight_unit"] or "st").lower(),
                    "old_price": round(old, 2),
                    "new_price": round(new, 2),
                    "change": round(new - old, 2),
                    "pct": round((new / old - 1) * 100, 1),
                    "first": obs[0]["local_date"],
                    "last": obs[-1]["local_date"],
                    "points": len(obs),
                }
            )
        out.sort(key=lambda x: x["pct"], reverse=True)
        return out[:limit]

    def inflation(self) -> list[dict[str, Any]]:
        """Basket index per month (or day): mean price ratio vs each product's first observed price."""
        f, a = self._filter()
        rows = self.conn.execute(
            f"""SELECT l.retailer || ':' || l.product_key AS k, substr(p.local_date, 1, {self._n}) AS month,
                       l.unit_price
                FROM purchase_lines l JOIN purchases p ON p.id = l.purchase_id
                WHERE l.product_key IS NOT NULL AND l.product_key NOT IN ('stamps') AND l.unit_price > 0 {f}
                ORDER BY p.ts_utc""",
            a,
        ).fetchall()
        base: dict[str, float] = {}
        ratios: dict[str, list[float]] = defaultdict(list)
        for r in rows:
            b = base.setdefault(r["k"], r["unit_price"])
            ratios[r["month"]].append(r["unit_price"] / b)
        return [
            {"month": m, "index": round(statistics.mean(v) * 100, 1), "n": len(v)} for m, v in sorted(ratios.items())
        ]

    def stores(self) -> list[dict[str, Any]]:
        f, a = self._filter()
        return self._rows(
            f"""SELECT retailer, store_id, MAX(store_street) AS street, MAX(store_postcode) AS postcode,
                       MAX(store_city) AS city, COUNT(*) AS visits, ROUND(SUM({_SPEND}), 2) AS spend,
                       MIN(local_date) AS first, MAX(local_date) AS last
                FROM purchases p WHERE channel = 'store' {f}
                GROUP BY retailer, store_id ORDER BY visits DESC""",
            a,
        )

    def weekdays(self) -> list[dict[str, Any]]:
        f, a = self._filter()
        return self._rows(
            f"""SELECT CAST(strftime('%w', local_date) AS INTEGER) AS dow, COUNT(*) AS baskets,
                       ROUND(SUM({_SPEND}), 2) AS spend FROM purchases p WHERE 1=1 {f} GROUP BY dow ORDER BY dow""",
            a,
        )

    def discounts_by_type(self) -> list[dict[str, Any]]:
        f, a = self._filter()
        return self._rows(
            f"""SELECT COALESCE(d.type, '?') AS type, COUNT(*) AS n, ROUND(SUM(d.amount), 2) AS saved
                FROM discounts d JOIN purchases p ON p.id = d.purchase_id WHERE 1=1 {f}
                GROUP BY 1 ORDER BY saved DESC""",
            a,
        )

    def purchases(self, limit: int = 50) -> list[dict[str, Any]]:
        f, a = self._filter()
        return self._rows(
            f"""SELECT p.id, p.retailer, p.account_id, a.label AS account, channel, local_date, store_street,
                       store_city, ROUND({_SPEND}, 2) AS spend, discount_total AS saved, item_count
                FROM purchases p LEFT JOIN accounts a ON a.id = p.account_id WHERE 1=1 {f}
                ORDER BY ts_utc DESC LIMIT ?""",
            [*a, limit],
        )

    def drill(
        self,
        period: str | None = None,
        category: str | None = None,
        channel: str | None = None,
        other_than: list[str] | None = None,
        store: str | None = None,
        with_lines: bool = False,
    ) -> dict[str, Any]:
        """What is behind one bar of a chart: the products (summed) and the purchases.

        `period`: a month (2026-11) or a day (2026-11-28); `category`: one category (the unknown
        bucket is `UNKNOWN`); `other_than`: every category except these (the "Overig" bar);
        `channel`: store or online; `store`: a store id ('' for purchases without one).
        Purchase spend is the spend in the selection: with a category, only those lines.
        `with_lines`: every purchase also carries its receipt lines (in the selection) and discounts.
        """
        f, a = self._filter()
        where, args = "", list(a)
        if period:
            where += f" AND substr(p.local_date, 1, {len(period)}) = ?"
            args.append(period)
        if channel:
            where += " AND p.channel = ?"
            args.append(channel)
        if store is not None:
            where += " AND COALESCE(p.store_id, '') = ?"
            args.append(store)
        cat = f"COALESCE(pr.category, '{UNKNOWN}')"
        line_where = ""
        if category:
            line_where += f" AND {cat} = ?"
            args.append(category)
        if other_than:
            line_where += f" AND {cat} NOT IN ({','.join('?' * len(other_than))})"
            args.extend(other_than)
        base = f"""FROM purchase_lines l JOIN purchases p ON p.id = l.purchase_id
                   LEFT JOIN products pr ON pr.retailer = l.retailer AND pr.product_key = l.product_key
                   WHERE l.amount > 0 AND COALESCE(l.product_key, '') NOT IN ('stamps') {f} {where} {line_where}"""
        items = self._rows(
            f"""SELECT l.retailer, l.product_key, COALESCE(pr.title, MAX(l.name)) AS name, {cat} AS category,
                       pr.unit_size, COUNT(DISTINCT l.purchase_id) AS times,
                       ROUND(SUM(CASE WHEN l.weight IS NULL THEN l.quantity ELSE 0 END), 1) AS units,
                       ROUND(SUM(l.weight), 3) AS weight, ROUND(SUM(l.amount), 2) AS spend,
                       MAX(p.local_date) AS last,
                       GROUP_CONCAT(DISTINCT CASE WHEN p.channel = 'online' THEN 'Online'
                                                  ELSE p.store_street END) AS stores
                {base}
                GROUP BY l.retailer, COALESCE(l.product_key, l.name) ORDER BY spend DESC""",
            args,
        )
        filtered = bool(category or other_than)
        purchases = self._rows(
            f"""SELECT p.id, p.retailer, p.channel, p.local_date, p.store_street, p.store_city,
                       a.label AS account, p.item_count, p.discount_total AS saved,
                       ROUND({"SUM(l.amount)" if filtered else _SPEND}, 2) AS spend, COUNT(*) AS lines
                {base.replace("WHERE", "LEFT JOIN accounts a ON a.id = p.account_id WHERE", 1)}
                GROUP BY p.id ORDER BY p.ts_utc DESC""",
            args,
        )
        if with_lines and purchases:
            by_id = {p["id"]: p for p in purchases}
            for p in purchases:
                p["lines"], p["discounts"] = [], []
            for r in self._rows(
                f"""SELECT l.purchase_id, l.retailer, l.product_key, COALESCE(pr.title, l.name) AS name,
                           l.name AS receipt_name,
                           {cat} AS category, l.quantity, l.weight, l.weight_unit, l.unit_price, l.amount, l.is_bonus
                    {base} ORDER BY l.purchase_id, l.line_no""",
                args,
            ):
                by_id[r.pop("purchase_id")]["lines"].append(r)
            if not filtered:
                marks = ",".join("?" * len(by_id))
                for r in self._rows(
                    f"SELECT purchase_id, name, type, amount FROM discounts WHERE purchase_id IN ({marks}) "
                    "ORDER BY idx",
                    list(by_id),
                ):
                    by_id[r.pop("purchase_id")]["discounts"].append(r)
                for p in purchases:
                    attach_discounts(p["lines"], p["discounts"])
        return {
            "items": items,
            "purchases": purchases,
            "spend": round(sum(i["spend"] for i in items), 2),
            "receipt_spend": round(sum(p["spend"] for p in purchases), 2),
        }

    def purchase(self, purchase_id: str) -> dict[str, Any] | None:
        head = self._rows("SELECT * FROM purchases WHERE id = ?", (purchase_id,))
        if not head:
            return None
        lines = self._rows(
            """SELECT l.*, COALESCE(pr.title, l.name) AS title, pr.category FROM purchase_lines l
               LEFT JOIN products pr ON pr.retailer = l.retailer AND pr.product_key = l.product_key
               WHERE l.purchase_id = ? ORDER BY l.line_no""",
            (purchase_id,),
        )
        discounts = self._rows("SELECT * FROM discounts WHERE purchase_id = ? ORDER BY idx", (purchase_id,))
        attach_discounts(lines, discounts, title="title")
        return {
            **head[0],
            "lines": lines,
            "discounts": discounts,
            "payments": self._rows("SELECT * FROM payments WHERE purchase_id = ? ORDER BY idx", (purchase_id,)),
        }

    def tldr(self) -> dict[str, Any]:
        ov = self.overview()
        return {
            "overview": {k: v for k, v in ov.items() if k != "last_sync"},
            "top_categories": [(c["category"], c["spend"]) for c in self.categories()[:5]],
            "top_products_by_spend": [(p["name"], p["spend"], p["times"]) for p in self.products("spend", 10)],
            "top_products_by_count": [(p["name"], p["times"]) for p in self.products("count", 10)],
            "biggest_price_increases": [
                (c["name"], c["old_price"], c["new_price"], c["pct"], c["per"]) for c in self.price_changes(limit=5)
            ],
            "discounts_by_type": self.discounts_by_type(),
            "market_changes_top10": [
                (c["name"], c["first_month"], c["old_price"], c["last_month"], c["new_price"], c["pct"])
                for c in self.market_changes(10)
            ],
            "bank_months": self.bank_months(),
        }


def _squash(text: str | None) -> str:
    """Letters and digits only, upper case: 'AH Witte druiven' -> 'AHWITTEDRUIVEN'."""
    return re.sub(r"[^0-9A-Z]", "", (text or "").upper())


def _common_run(a: str, b: str) -> int:
    """Length of the longest common substring."""
    best, prev = 0, [0] * (len(b) + 1)
    for ca in a:
        cur = [0] * (len(b) + 1)
        for j, cb in enumerate(b, 1):
            if ca == cb:
                cur[j] = prev[j - 1] + 1
                best = max(best, cur[j])
        prev = cur
    return best


def attach_discounts(lines: list[dict[str, Any]], discounts: list[dict[str, Any]], title: str = "name") -> None:
    """Link receipt-level discounts to the line they are for.

    AH lists Bonus discounts per receipt under a cut-short name without spaces ('AHWITTEDRUIV' for
    'AH WITTE DRUIVEN'). Each discount goes to the line whose receipt name or title shares the longest
    run of letters with it (at least 5), preferring Bonus lines; when one discount and one Bonus line are
    left, they belong together. Sets `discount` (total) and `discount_names` on lines and `line` (the
    line's index) on discounts; unmatched discounts keep `line` = None.
    """
    for ln in lines:
        ln["discount"], ln["discount_names"] = 0.0, []
    pairs = []
    for di, d in enumerate(discounts):
        dn = _squash(d.get("name"))
        if len(dn) < 4:
            continue
        for li, ln in enumerate(lines):
            if not ln.get("product_key") or (ln.get("amount") or 0) <= 0:
                continue
            names = {_squash(ln.get("receipt_name") or ln.get("name")), _squash(ln.get(title))}
            names |= {n[2:] for n in names if n.startswith("AH")}
            run = max(_common_run(dn, n) for n in names if n) if any(names) else 0
            if run >= 5 or (run >= 4 and run == len(dn)):
                pairs.append((run, bool(ln.get("is_bonus")), di, li))
    taken_d, taken_l = set(), set()
    for d in discounts:
        d["line"] = None
    for _run, _bonus, di, li in sorted(pairs, reverse=True):
        if di in taken_d or li in taken_l:
            continue
        taken_d.add(di)
        taken_l.add(li)
        discounts[di]["line"] = li
    left_d = [i for i, d in enumerate(discounts) if d["line"] is None and (d.get("amount") or 0) > 0]
    left_l = [i for i, ln in enumerate(lines) if ln.get("is_bonus") and i not in taken_l]
    if len(left_d) == 1 and len(left_l) == 1:
        discounts[left_d[0]]["line"] = left_l[0]
    for d in discounts:
        if d["line"] is not None:
            ln = lines[d["line"]]
            ln["discount"] = round(ln["discount"] + (d.get("amount") or 0), 2)
            ln["discount_names"].append(d.get("name"))


def _pack_kg(size: str) -> float | None:
    """Pack weight in kg from a size like 'ca. 320 g', '1,5 kg' or '500 g'."""
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*(kg|g)\b", size)
    if not m:
        return None
    v = float(m.group(1).replace(",", "."))
    return v if m.group(2) == "kg" else v / 1000


def _month_index(month: str) -> int:
    return int(month[:4]) * 12 + int(month[5:7]) - 1


def clean_shelf_prices(rows: list[Any], paid: dict[str, list[float]]) -> tuple[list[dict[str, Any]], str | None]:
    """Monthly shelf-price series from public samples, with dataset artefacts removed.

    Per month the highest sample is used (samples can be one-day promotion prices). Points are then
    blanked (None, drawn as gaps) when they are outliers against the product's whole series (the
    dataset sometimes halves a price for months), against the three months on either side, or
    against what the receipts show within two months (more than 15% below or 20% above). If the
    series disagrees with the receipts in
    most months it describes another variant, and nothing is returned.
    """
    by_month: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        by_month[r["month"]].append(r["price"])
    months = sorted(by_month)
    if not months:
        return [], None
    prices = {m: max(by_month[m]) for m in months}
    overall = statistics.median(prices.values())
    idx = {m: _month_index(m) for m in months}
    paid_idx: dict[int, list[float]] = defaultdict(list)
    for m, v in paid.items():
        paid_idx[_month_index(m)].extend(v)
    keep: dict[str, float | None] = {}
    for m in months:
        p = prices[m]
        neighbours = [prices[n] for n in months if n != m and abs(idx[n] - idx[m]) <= 3]
        near_paid = [x for d in range(-2, 3) for x in paid_idx.get(idx[m] + d, [])]
        outlier = (
            p < 0.7 * overall
            or p > 1.5 * overall
            or (neighbours and p < 0.85 * statistics.median(neighbours))
            or (near_paid and not 0.85 <= p / statistics.median(near_paid) <= 1.2)
        )
        keep[m] = None if outlier else round(p, 2)
    overlap = [(prices[m], statistics.median(paid_idx[idx[m]])) for m in months if paid_idx.get(idx[m])]
    if overlap and sum(abs(s / p - 1) > 0.2 for s, p in overlap) > len(overlap) / 2:
        return [], "the public data for this product does not match your receipts (likely another variant)"
    # short runs (up to four months) between blanked points, or trailing after one, read as
    # floating dashes: drop them when the series has a longer run to show
    runs: list[tuple[int, int]] = []
    i = 0
    while i < len(months):
        if keep[months[i]] is None:
            i += 1
            continue
        j = i
        while j + 1 < len(months) and keep[months[j + 1]] is not None and idx[months[j + 1]] - idx[months[j]] == 1:
            j += 1
        runs.append((i, j))
        i = j + 1
    if any(j - i + 1 > 4 for i, j in runs):
        before = dict(keep)  # judge every run against the series as it was, not as runs get dropped
        for i, j in runs:
            # a gap is a blanked month or a month missing from the dataset
            gap_before = i > 0 and (before[months[i - 1]] is None or idx[months[i]] - idx[months[i - 1]] > 1)
            gap_after = j + 1 < len(months) and (
                before[months[j + 1]] is None or idx[months[j + 1]] - idx[months[j]] > 1
            )
            # how many months back to the previous kept point
            prev_kept = next((idx[months[k]] for k in range(i - 1, -1, -1) if before[months[k]] is not None), None)
            long_gap_before = prev_kept is not None and idx[months[i]] - prev_kept > 2
            trailing = j == len(months) - 1 and long_gap_before
            if j - i + 1 <= 4 and gap_before and (gap_after or trailing):
                for m in months[i : j + 1]:
                    keep[m] = None
    return [{"date": m + "-15", "price": keep[m], "samples": len(by_month[m])} for m in months], None
