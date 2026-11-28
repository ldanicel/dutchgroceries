from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from groceries import sync
from groceries.analytics import UNKNOWN, Analytics
from groceries.config import Config
from groceries.db import connect
from groceries.web import create_app

from .conftest import FakeProvider


@pytest.fixture
def synced(config: Config, monkeypatch: pytest.MonkeyPatch) -> FakeProvider:
    provider = FakeProvider(config)
    monkeypatch.setattr(sync, "get_provider", lambda slug, cfg, account=None: provider)
    sync.run_sync(config, "ah")
    return provider


def test_drill_matches_the_bars(config: Config, synced: FakeProvider) -> None:
    a = Analytics(connect(config.db_path))
    # every category bar: its drill-down adds up to the bar
    for c in a.categories():
        d = a.drill(category=c["category"])
        assert d["spend"] == c["spend"]
        assert sum(i["times"] for i in d["items"]) >= len(d["purchases"]) > 0
    # every month bar, per channel
    for m in a.months():
        for channel in ("store", "online"):
            if m[channel]:
                d = a.drill(period=m["month"], channel=channel)
                assert d["receipt_spend"] == m[channel]
    # "Overig": everything except the named categories
    cats = [c["category"] for c in a.categories()]
    rest = a.drill(other_than=cats[:1])
    assert round(rest["spend"], 2) == round(sum(c["spend"] for c in a.categories()[1:]), 2)
    assert a.drill(category="no such category") == {"items": [], "purchases": [], "spend": 0, "receipt_spend": 0}
    assert all(i["category"] != "Koopzegels" for i in a.drill()["items"])
    assert UNKNOWN  # the unknown bucket is drillable by its label


def test_drill_by_store_and_day(config: Config, synced: FakeProvider) -> None:
    a = Analytics(connect(config.db_path))
    [store] = a.stores()
    d = a.drill(store=store["store_id"] or "")
    assert len(d["purchases"]) == store["visits"]
    day = d["purchases"][0]["local_date"]
    assert a.drill(period=day)["purchases"][0]["local_date"] == day


def test_drill_api(config: Config, synced: FakeProvider) -> None:
    client = TestClient(create_app(config))
    r = client.get("/api/drill", params={"period": "2025-04", "channel": "online"}).json()
    assert len(r["purchases"]) == 1 and r["items"]
    r = client.get("/api/drill", params=[("other_than", "Kaas"), ("other_than", "Zuivel, eieren")]).json()
    assert all(i["category"] not in ("Kaas", "Zuivel, eieren") for i in r["items"])
    assert client.get("/api/drill", params={"period": "2025"}).status_code == 422
    assert client.get("/api/drill", params={"channel": "bike"}).status_code == 422
    assert client.get("/api/drill", params={"range": "week"}).json()["purchases"] == []


def test_discounts_attach_to_their_line() -> None:
    from groceries.analytics import attach_discounts

    lines = [
        {"product_key": "1", "receipt_name": "CHOCO CAKE", "name": "Example Choco cake", "amount": 2.97},
        {
            "product_key": "2",
            "receipt_name": "WITTE DRUIVEN",
            "name": "Example Witte druiven",
            "amount": 2.59,
            "is_bonus": 1,
        },  # fmt: skip
        {
            "product_key": "3",
            "receipt_name": "JAZZ APPELS",
            "name": "Jazz Appels schaal",
            "amount": 3.49,
            "is_bonus": 1,
        },
        {
            "product_key": "4",
            "receipt_name": "JONG BELEGEN",
            "name": "Example Jong belegen",
            "amount": 4.44,
            "is_bonus": 1,
        },  # fmt: skip
    ]
    discounts = [
        {"name": "AHWITTEDRUIV", "amount": 0.70},
        {"name": "JAZZAPPELSSC", "amount": 0.50},
        {"name": "BIOPREMIUMWI", "amount": 0.44},  # no name match: the one Bonus line left
        {"name": "KOOPZEGELS", "amount": 0},
    ]
    attach_discounts(lines, discounts)
    assert [d["line"] for d in discounts] == [1, 2, 3, None]
    assert [ln["discount"] for ln in lines] == [0, 0.70, 0.50, 0.44]
    assert lines[1]["discount_names"] == ["AHWITTEDRUIV"]


def test_deals_over_several_products_are_split() -> None:
    from groceries.analytics import attach_discounts

    lines = [
        {"product_key": "1", "receipt_name": "LU KOEK A", "name": "Example LU koek A", "amount": 2.0, "is_bonus": 1},
        {"product_key": "2", "receipt_name": "LU KOEK B", "name": "Example LU koek B", "amount": 3.0, "is_bonus": 1},
        {"product_key": "3", "receipt_name": "BIO MELK", "name": "AH Biologisch Melk", "amount": 1.5, "is_bonus": 1},
        {
            "product_key": "4",
            "receipt_name": "BIO EIEREN",
            "name": "AH Biologisch Eieren",
            "amount": 4.5,
            "is_bonus": 1,
        },  # fmt: skip
        {"product_key": "5", "receipt_name": "BROOD", "name": "Example Brood", "amount": 2.5},
    ]
    discounts = [
        {"name": "ALLELUKOEK", "amount": 1.0},
        {"name": "BIOPREMIUMST", "amount": 0.61},
        {"name": "MIJN AH MILES", "amount": 1.0},
    ]
    attach_discounts(lines, discounts)
    assert discounts[0]["split"] == [{"line": 0, "amount": 0.4}, {"line": 1, "amount": 0.6}]
    assert [p["line"] for p in discounts[1]["split"]] == [2, 3]
    assert round(sum(p["amount"] for p in discounts[1]["split"]), 2) == 0.61
    assert discounts[2]["line"] is None  # a credit for no product stays on the receipt
    assert lines[4]["discount"] == 0
