from __future__ import annotations

import pytest

from groceries.providers.ah import STAMPS_KEY, extract_code, parse_order, parse_product, parse_receipt

from .conftest import load


def test_receipt_reconciles_with_total() -> None:
    r = load("ah_receipt.json")
    p = parse_receipt(r["summary"], r["detail"])
    assert p.id == "ah:store:AHtest0000000000000000000000000000000001"
    lines = sum(ln.amount for ln in p.lines) + sum(ln.deposit or 0 for ln in p.lines)
    assert round(lines - p.discount_total, 2) == p.total == 21.25
    assert p.store_id == "9999"
    assert p.store_street == "Voorbeeldstraat 1"


def test_receipt_lines() -> None:
    r = load("ah_receipt.json")
    lines = {ln.name: ln for ln in parse_receipt(r["summary"], r["detail"]).lines}
    assert lines["TEST MELK"].unit_price == 1.25 and lines["TEST MELK"].is_bonus
    assert lines["TEST KAAS"].unit_price == 6.0 and not lines["TEST KAAS"].is_bonus
    assert lines["TEST APPELS"].unit_price == 3.0 and lines["TEST APPELS"].weight_unit == "KG"
    assert lines["TEST STEMPEL"].product_key is None
    assert lines["Koopzegels"].product_key == STAMPS_KEY and lines["Koopzegels"].amount == 11.0


def test_personal_discount_not_counted_twice() -> None:
    r = load("ah_receipt.json")
    p = parse_receipt(r["summary"], r["detail"])
    assert p.discount_total == 0.9
    assert [d.type for d in p.discounts] == ["BONUS", "BBOX"]


def test_order_skips_undelivered_and_keys_on_pos_id() -> None:
    o = load("ah_order.json")
    p = parse_order(o["summary"], o["detail"])
    assert p.channel == "online" and p.external_id == "123456789"
    assert p.ts_utc == "2025-04-02T06:00:00Z"
    assert [(ln.product_key, ln.amount) for ln in p.lines] == [("100001", 3.0), ("100002", 7.0)]
    assert p.total == 13.95 and p.discount_total == 1.0


def test_parse_product_splits_category() -> None:
    info = parse_product("100001", 500001, {"id": 500001, "title": "X", "category": "Zuivel, eieren/Melk"})
    assert (info.category, info.subcategory, info.lookup_state) == ("Zuivel, eieren", "Melk", "ok")
    assert parse_product("1", 2, None).lookup_state == "missing"


@pytest.mark.parametrize(
    "pasted",
    ["abc-123", "appie://login-exit?code=abc-123", " 'appie://login-exit?code=abc-123&state=x' ", "?code=abc-123"],
)
def test_extract_code(pasted: str) -> None:
    assert extract_code(pasted) == "abc-123"
