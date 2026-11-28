from __future__ import annotations

import sqlite3
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from groceries import sync
from groceries.analytics import Analytics, range_start
from groceries.config import Config
from groceries.db import SCHEMA, connect
from groceries.web import create_app

from .conftest import FakeProvider


@pytest.mark.parametrize(
    ("today", "preset", "start"),
    [
        (date(2026, 11, 28), "week", date(2026, 11, 22)),
        (date(2026, 11, 28), "month", date(2026, 10, 29)),
        (date(2026, 11, 28), "quarter", date(2026, 8, 29)),
        (date(2026, 11, 28), "year", date(2025, 11, 29)),
        (date(2026, 11, 28), "2y", date(2024, 11, 29)),
        (date(2026, 3, 31), "month", date(2026, 3, 1)),  # 31 Feb does not exist: clamp, then the day after
        (date(2026, 1, 15), "month", date(2025, 12, 16)),  # across the year boundary
        (date(2024, 2, 29), "year", date(2023, 3, 1)),
    ],
)
def test_range_start(today: date, preset: str, start: date) -> None:
    assert range_start(preset, today) == start


def test_range_start_all_and_unknown() -> None:
    assert range_start(None) is None and range_start("all") is None
    with pytest.raises(ValueError):
        range_start("decade")


def _db(days_ago: list[int]) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    for i, ago in enumerate(days_ago):
        day = (date.today() - timedelta(days=ago)).isoformat()
        pid = f"ah:store:{i}"
        conn.execute(
            "INSERT INTO purchases (id, retailer, channel, external_id, ts_utc, local_date, total, item_count) "
            "VALUES (?, 'ah', 'store', ?, ?, ?, 10, 1)",
            (pid, str(i), day + "T10:00:00Z", day),
        )
        conn.execute(
            "INSERT INTO purchase_lines (purchase_id, line_no, retailer, product_key, name, unit_price, amount) "
            "VALUES (?, 0, 'ah', '42', 'X', 10, 10)",
            (pid,),
        )
        conn.execute(
            "INSERT INTO discounts (purchase_id, idx, type, amount) VALUES (?, 0, 'bonus', 1)",
            (pid,),
        )
    conn.execute(
        "INSERT INTO bank_payments (id, retailer, date, amount, description) VALUES ('b1', 'ah', ?, 50, 'card')",
        ((date.today() - timedelta(days=900)).isoformat(),),
    )
    return conn


def test_every_query_follows_the_range() -> None:
    conn = _db([0, 3, 20, 60, 200, 500])
    counts = {}
    for preset in ("week", "month", "quarter", "year", "2y", "all"):
        a = Analytics(conn, since=range_start(preset))
        counts[preset] = a.overview()["purchases"]
        assert sum(m["baskets"] for m in a.months()) == counts[preset]
        assert a.categories()[0]["lines"] == counts[preset]
        assert a.products()[0]["times"] == counts[preset]
        assert len(a.purchases()) == counts[preset]
        assert sum(w["baskets"] for w in a.weekdays()) == counts[preset]
        assert a.stores()[0]["visits"] == counts[preset]
        assert a.discounts_by_type()[0]["n"] == counts[preset]
        assert len(a.price_history("ah", "42")["points"]) == counts[preset]
        assert sum(m["n"] for m in a.inflation()) == counts[preset]
    assert counts == {"week": 2, "month": 3, "quarter": 4, "year": 5, "2y": 6, "all": 6}
    # bank payments (before the first receipt) only count when the period reaches back to them
    assert Analytics(conn, since=range_start("2y")).bank_months() == []
    assert len(Analytics(conn).bank_months()) == 1


def test_price_changes_scale_to_the_period() -> None:
    conn = _db([1, 10, 20, 28])
    a = Analytics(conn, since=range_start("month"))
    # 27 days of history: too short for the 120-day default, enough for half of a month
    assert Analytics(conn).price_changes() == []
    assert a.price_changes()[0]["points"] == 4


@pytest.fixture
def synced(config: Config, monkeypatch: pytest.MonkeyPatch) -> FakeProvider:
    provider = FakeProvider(config)
    monkeypatch.setattr(sync, "get_provider", lambda slug, cfg, account=None: provider)
    sync.run_sync(config, "ah")
    return provider


def test_web_range_param(config: Config, synced: FakeProvider) -> None:
    client = TestClient(create_app(config))
    all_time = client.get("/api/overview", params={"range": "all"}).json()
    assert all_time["purchases"] == 2 and all_time["since"] is None
    # the fixtures are from 2025: nothing in the last week
    week = client.get("/api/overview", params={"range": "week"}).json()
    assert week["purchases"] == 0 and week["since"] == range_start("week").isoformat()
    assert client.get("/api/purchases", params={"range": "week"}).json() == []
    hist = client.get("/api/price-history", params={"retailer": "ah", "key": "100001", "range": "week"}).json()
    assert hist["points"] == [] and hist["since"]
    assert client.get("/api/overview", params={"range": "decade"}).status_code == 422
    assert connect(config.db_path)  # db untouched by read-only queries


def test_short_periods_are_grouped_per_day() -> None:
    conn = _db([0, 0, 3, 20])
    week = Analytics(conn, since=range_start("week"), bucket="day")
    days = week.months()
    # every day of the period, including the ones without purchases
    assert [d["month"] for d in days] == [(date.today() - timedelta(days=i)).isoformat() for i in range(6, -1, -1)]
    assert days[-1]["baskets"] == 2 and days[-4]["baskets"] == 1 and days[0]["baskets"] == 0
    assert len(week.category_months()["months"]) == 7
    assert [r["month"] for r in week.inflation()][-1] == date.today().isoformat()
    # the overview still averages per calendar month
    assert week.overview()["months"] == len({(date.today() - timedelta(days=i)).isoformat()[:7] for i in (0, 3)})
    with pytest.raises(ValueError):
        Analytics(conn, bucket="hour")


def test_web_short_range_is_per_day(config: Config, synced: FakeProvider) -> None:
    client = TestClient(create_app(config))
    assert len(client.get("/api/months", params={"range": "week"}).json()) == 7
    assert all(len(m["month"]) == 7 for m in client.get("/api/months", params={"range": "year"}).json())
