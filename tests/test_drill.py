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
