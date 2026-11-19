"""Synthetic demo data: `groceries demo-seed` fills an empty data dir with made-up purchases.

Nothing here comes from a real account: stores, products, prices and dates are invented
(with a deterministic random seed), so screenshots and demos never show anyone's shopping.
"""

from __future__ import annotations

import random
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from .db import connect
from .providers.base import Discount, Line, Payment, ProductInfo, Purchase
from .sync import store_product, store_purchase

# (key, name, category, base price, weekly chance, yearly inflation)
CATALOGUE = [
    ("1001", "Demo milk", "Zuivel, eieren", 1.13, 0.8, 0.05),
    ("1002", "Demo cheese slices", "Kaas", 3.17, 0.5, 0.09),
    ("1003", "Demo grated cheese", "Kaas", 2.41, 0.45, 0.06),
    ("1004", "Demo bananas", "Fruit, verse sappen", 1.62, 0.7, 0.03),
    ("1005", "Demo berries", "Fruit, verse sappen", 2.83, 0.35, 0.12),
    ("1006", "Demo bread", "Bakkerij", 2.27, 0.7, 0.07),
    ("1007", "Demo ham", "Vleeswaren", 2.71, 0.4, 0.08),
    ("1008", "Demo chicken", "Vlees", 6.33, 0.35, 0.04),
    ("1009", "Demo pasta", "Pasta, rijst, wereldkeuken", 1.04, 0.3, 0.10),
    ("1010", "Demo sauce", "Soepen, sauzen, kruiden, olie", 1.66, 0.3, 0.06),
    ("1011", "Demo cookies", "Koek, snoep, chocolade", 2.14, 0.4, 0.18),
    ("1012", "Demo vegetables", "Groente, aardappelen", 2.06, 0.5, 0.02),
    ("1013", "Demo yoghurt", "Zuivel, eieren", 1.37, 0.5, 0.05),
    ("1014", "Demo coffee", "Koffie, thee", 7.73, 0.15, 0.15),
    ("1015", "Demo detergent", "Huishouden", 9.61, 0.08, 0.04),
    ("1016", "Demo crisps", "Borrel, chips, snacks", 1.86, 0.3, 0.11),
    ("1017", "Demo fish", "Vis", 5.77, 0.2, 0.06),
    ("1018", "Demo toothpaste", "Drogisterij", 2.33, 0.1, 0.08),
]
STORES = {
    "ah": [("9001", "Voorbeeldstraat 1", "1234AB", "Demostad"), ("9002", "Proefplein 12", "1234CD", "Demostad")],
    "jumbo": [("9101", "Testlaan 7", "5678EF", "Demodorp")],
}


def _price(base: float, inflation: float, day: date, start: date) -> float:
    years = (day - start).days / 365
    return round(base * (1 + inflation) ** years, 2)


def seed(data_dir: Path, months: int = 24, rng_seed: int = 7) -> dict[str, int]:
    rng = random.Random(rng_seed)
    db = data_dir / "groceries.db"
    if db.exists():
        raise SystemExit(f"{db} already exists; demo-seed only writes to an empty data dir")
    conn = connect(db)
    end = date.today()
    start = end - timedelta(days=months * 30)
    counts = {"purchases": 0, "market_prices": 0, "bank_payments": 0}
    day, n = start, 0
    while day <= end:
        for retailer, every in (("ah", 2), ("jumbo", 9)):
            if (day - start).days % every or (retailer == "jumbo" and day < start + timedelta(days=200)):
                continue
            lines = []
            for key, name, _cat, base, chance, infl in CATALOGUE:
                if rng.random() < chance * (1.1 if retailer == "ah" else 0.9):
                    qty = 1 if rng.random() < 0.8 else 2
                    unit = _price(base, infl, day, start) + (0.10 if retailer == "jumbo" else 0)
                    lines.append(
                        Line(
                            name=name,
                            amount=round(unit * qty, 2),
                            quantity=qty,
                            product_key=key,
                            unit_price=unit,
                            is_bonus=rng.random() < 0.2,
                        )
                    )
            if not lines:
                continue
            gross = sum(ln.amount for ln in lines)
            saved = round(gross * rng.uniform(0, 0.12), 2)
            store = rng.choice(STORES[retailer])
            n += 1
            ts = datetime(day.year, day.month, day.day, 16, rng.randint(0, 59), tzinfo=UTC)
            store_purchase(
                conn,
                Purchase(
                    retailer=retailer,
                    channel="online" if retailer == "ah" and n % 25 == 0 else "store",
                    external_id=f"demo-{retailer}-{n:05d}",
                    ts_utc=ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    total=round(gross - saved, 2),
                    lines=lines,
                    discounts=[Discount(amount=saved, name="Bonus", type="BONUS")] if saved else [],
                    payments=[Payment(method="PIN", amount=round(gross - saved, 2))],
                    store_id=store[0],
                    store_street=store[1],
                    store_postcode=store[2],
                    store_city=store[3],
                ),
                "Europe/Amsterdam",
                None,
            )
            counts["purchases"] += 1
        day += timedelta(days=1)
    with conn:
        for retailer in STORES:
            for key, name, cat, *_ in CATALOGUE:
                store_product(conn, retailer, ProductInfo(product_key=key, lookup_state="ok", title=name, category=cat))
        # public shelf prices from a year before the first receipt, weekly
        d = start - timedelta(days=365)
        while d <= end:
            for key, _n, _c, base, _ch, infl in CATALOGUE:
                conn.execute(
                    "INSERT OR IGNORE INTO market_prices (retailer, product_key, date, price, source) "
                    "VALUES ('ah', ?, ?, ?, 'demo')",
                    (key, d.isoformat(), _price(base, infl, d, start)),
                )
                counts["market_prices"] += 1
            d += timedelta(days=7)
        # bank card payments for the year before receipts begin
        d = start - timedelta(days=365)
        while d < start:
            conn.execute(
                "INSERT INTO bank_payments (id, retailer, date, amount, description) VALUES (?, 'ah', ?, ?, ?)",
                (f"demo-{d.isoformat()}", d.isoformat(), round(rng.uniform(30, 70), 2), "DEMO SUPERMARKT"),
            )
            counts["bank_payments"] += 1
            d += timedelta(days=4)
        conn.execute(
            "INSERT INTO sync_runs (retailer, started_at, finished_at, status, message) VALUES "
            "('ah', ?, ?, 'ok', 'demo data')",
            (datetime.now(UTC).isoformat(timespec="seconds"),) * 2,
        )
    return counts
