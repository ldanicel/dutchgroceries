from __future__ import annotations

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from groceries import sync
from groceries.analytics import Analytics
from groceries.config import Config
from groceries.db import SCHEMA, connect
from groceries.web import create_app

from .conftest import FakeProvider


@pytest.fixture
def synced(config: Config, monkeypatch: pytest.MonkeyPatch) -> FakeProvider:
    provider = FakeProvider(config)
    monkeypatch.setattr(sync, "get_provider", lambda slug, cfg: provider)
    sync.run_sync(config, "ah")
    return provider


def test_sync_stores_everything(config: Config, synced: FakeProvider) -> None:
    conn = connect(config.db_path)
    assert conn.execute("SELECT COUNT(*) FROM purchases").fetchone()[0] == 2
    assert (config.raw_dir / "ah" / "store" / "AHtest0000000000000000000000000000000001.json").is_file()
    # online order lines came with categories; only receipt-only products are looked up
    assert sorted(synced.looked_up) == ["100003", "100004"]
    run = conn.execute("SELECT status, new_purchases FROM sync_runs").fetchone()
    assert tuple(run) == ("ok", 2)


def test_second_sync_is_incremental(config: Config, synced: FakeProvider) -> None:
    res = sync.run_sync(config, "ah")
    assert (res.listed, res.fetched, res.products_enriched) == (2, 0, 0)


def test_analytics(config: Config, synced: FakeProvider) -> None:
    a = Analytics(connect(config.db_path))
    ov = a.overview()
    assert ov["purchases"] == 2
    assert ov["spend"] == round(21.25 - 11.0 + 13.95, 2)  # stamps excluded
    assert ov["stamps_bought"] == 11.0
    cats = {c["category"]: c["spend"] for c in a.categories()}
    assert cats["Kaas"] == 13.0 and "Koopzegels" not in cats
    months = {m["month"]: m for m in a.months()}
    assert months["2025-04"]["online"] == 13.95
    top = a.products("count", 5)
    assert top[0]["product_key"] in ("100001", "100002")


def test_price_changes() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    for i, (day, price) in enumerate(
        [("2025-01-01", 1.0), ("2025-02-01", 1.0), ("2025-06-01", 1.2), ("2025-07-01", 1.2)]
    ):
        pid = f"ah:store:{i}"
        conn.execute(
            "INSERT INTO purchases (id, retailer, channel, external_id, ts_utc, local_date, total) "
            "VALUES (?, 'ah', 'store', ?, ?, ?, ?)",
            (pid, str(i), day + "T10:00:00Z", day, price),
        )
        conn.execute(
            "INSERT INTO purchase_lines (purchase_id, line_no, retailer, product_key, name, unit_price, amount) "
            "VALUES (?, 0, 'ah', '42', 'X', ?, ?)",
            (pid, price, price),
        )
    [change] = Analytics(conn).price_changes(min_points=4, min_days=100)
    assert (change["old_price"], change["new_price"], change["pct"]) == (1.0, 1.2, 20.0)


def test_failure_alerts_once_then_resolves(config: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[str] = []
    monkeypatch.setattr("groceries.notify.Notifier.send", lambda self, title, *a, **k: sent.append(title) or True)
    provider = FakeProvider(config)
    monkeypatch.setattr(sync, "get_provider", lambda slug, cfg: provider)

    def boom() -> None:
        raise RuntimeError("API down")

    monkeypatch.setattr(provider, "check_auth", boom)
    for _ in range(2):
        with pytest.raises(RuntimeError):
            sync.run_sync(config, "ah")
    assert sent == ["⚠ AH sync failed"]
    assert "AH" in json.loads((config.data_dir / "alert-state.json").read_text())
    monkeypatch.setattr(provider, "check_auth", lambda: None)
    sync.run_sync(config, "ah")
    sync.run_sync(config, "ah")
    assert sent == ["⚠ AH sync failed", "✅ Resolved: AH sync"]


def test_web_api(config: Config, synced: FakeProvider) -> None:
    client = TestClient(create_app(config))
    assert client.get("/").status_code == 200
    assert client.get("/api/overview").json()["purchases"] == 2
    assert client.get("/api/overview", params={"retailer": "jumbo"}).json()["purchases"] == 0
    assert len(client.get("/api/purchases").json()) == 2
    hist = client.get("/api/price-history", params={"retailer": "ah", "key": "100001"}).json()
    assert len(hist["points"]) == 2
    assert client.get("/api/purchases/nope").status_code == 404


def test_basic_auth(config: Config, synced: FakeProvider) -> None:
    config.basic_auth = "me:secret"
    client = TestClient(create_app(config))
    assert client.get("/api/overview").status_code == 401
    assert client.get("/healthz").status_code == 200
    assert client.get("/api/overview", auth=("me", "secret")).status_code == 200


def test_normalize_quantities_repairs_multi_item_lines() -> None:
    from groceries.sync import normalize_quantities

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    rows = [
        ("2025-01-01", 2, 3.98),
        ("2025-01-10", 1, 1.99),
        ("2025-01-20", 1, 5.97),
        ("2025-02-01", 1, 3.98),
        ("2025-02-05", 1, 1.0),
    ]
    for i, (day, qty, amount) in enumerate(rows):
        pid = f"ah:store:{i}"
        conn.execute(
            "INSERT INTO purchases (id, retailer, channel, external_id, ts_utc, local_date, total) "
            "VALUES (?, 'ah', 'store', ?, ?, ?, ?)",
            (pid, str(i), day + "T10:00:00Z", day, amount),
        )
        conn.execute(
            "INSERT INTO purchase_lines (purchase_id, line_no, retailer, product_key, name, quantity, "
            "unit_price, amount) VALUES (?, 0, 'ah', '42', 'X', ?, ?, ?)",
            (pid, qty, amount / qty, amount),
        )
    conn.execute(
        "INSERT INTO purchases (id, retailer, channel, external_id, ts_utc, local_date, total) "
        "VALUES ('ah:store:s', 'ah', 'store', 's', '2025-01-01T10:00:00Z', '2025-01-01', 7.6)"
    )
    conn.execute(
        "INSERT INTO purchase_lines (purchase_id, line_no, retailer, product_key, name, quantity, unit_price, "
        "amount) VALUES ('ah:store:s', 0, 'ah', 'stamps', 'Koopzegels', 1, 7.6, 7.6)"
    )
    assert normalize_quantities(conn, "ah") == 2
    got = [
        tuple(r)
        for r in conn.execute(
            "SELECT quantity, unit_price, qty_inferred FROM purchase_lines "
            "WHERE product_key = '42' ORDER BY purchase_id"
        )
    ]
    assert got == [(2, 1.99, 0), (1, 1.99, 0), (3, 1.99, 1), (2, 1.99, 1), (1, 1.0, 0)]
    assert conn.execute("SELECT quantity FROM purchase_lines WHERE product_key = 'stamps'").fetchone()[0] == 1
    assert normalize_quantities(conn, "ah") == 0  # idempotent


def test_clean_shelf_prices_drops_artefacts() -> None:
    from groceries.analytics import clean_shelf_prices

    def rows(prices: dict[str, float]) -> list[dict]:
        return [{"month": m, "price": p} for m, p in prices.items()]

    months = [f"2024-{m:02d}" for m in range(1, 13)]
    flat = dict.fromkeys(months, 3.99)
    # a one-month dip and a halved plateau are dataset artefacts; a lasting rise is real
    series, note = clean_shelf_prices(
        rows({**flat, "2024-03": 2.99, "2024-06": 1.99, "2024-07": 1.99, "2024-11": 4.49, "2024-12": 4.49}), {}
    )
    got = {x["date"][:7]: x["price"] for x in series}
    assert note is None
    assert got["2024-03"] is None and got["2024-06"] is None and got["2024-07"] is None
    assert got["2024-01"] == 3.99 and got["2024-12"] == 4.49
    # below what the receipts show around that month: gap even without a purchase that month
    series, _ = clean_shelf_prices(rows({**flat, "2024-05": 3.29}), {"2024-04": [3.99], "2024-06": [3.99]})
    assert {x["date"][:7]: x["price"] for x in series}["2024-05"] is None
    # disagreeing with the receipts in most months: another variant, no line at all
    series, note = clean_shelf_prices(rows(flat), {m: [2.49] for m in months})
    assert series == [] and note


def test_clean_shelf_prices_drops_orphan_fragments() -> None:
    from groceries.analytics import clean_shelf_prices

    months = [f"2024-{m:02d}" for m in range(1, 13)]
    prices = dict.fromkeys(months, 3.99)
    prices["2024-06"] = 1.0  # becomes a gap ...
    prices["2024-08"] = 1.0  # ... and so does this, leaving 2024-07 alone between them
    series, _ = clean_shelf_prices([{"month": m, "price": p} for m, p in prices.items()], {})
    got = {x["date"][:7]: x["price"] for x in series}
    assert got["2024-07"] is None and got["2024-05"] == 3.99 and got["2024-09"] == 3.99
