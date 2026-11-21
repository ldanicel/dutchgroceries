"""Receipt layouts of the Jumbo print document, and matching search hits to receipt names.
All receipts in jumbo_receipt_layouts.json are made up."""

from __future__ import annotations

import json
import logging

import httpx
import pytest

from groceries.config import Config
from groceries.providers.jumbo import Jumbo, best_hit, parse_receipt, receipt_text_lines, title_matches

from .conftest import load

LAYOUTS = load("jumbo_receipt_layouts.json")


@pytest.mark.parametrize("name", sorted(LAYOUTS))
def test_layout(name: str, caplog: pytest.LogCaptureFixture) -> None:
    case = LAYOUTS[name]
    with caplog.at_level(logging.WARNING):
        p = parse_receipt(case["summary"], case["detail"])
    assert p.total == case["total"]
    assert len(p.lines) == case["lines"]
    assert [d.amount for d in p.discounts] == case["discounts"]
    assert [[pm.method, pm.amount] for pm in p.payments] == case["payments"]
    if case.get("mismatch"):
        assert case["summary"]["transactionId"] in caplog.text
    else:
        assert round(sum(ln.amount for ln in p.lines) - p.discount_total, 2) == p.total
        assert round(sum(pm.amount for pm in p.payments), 2) == p.total
        assert not caplog.records


def test_mixed_layout_details() -> None:
    case = LAYOUTS["padded_mixed"]
    by = {ln.name: ln for ln in parse_receipt(case["summary"], case["detail"]).lines}
    assert by["TEST APPELS"].weight == 0.512 and by["TEST APPELS"].weight_unit == "KG"
    assert by["TEST YOGHURT"].quantity == 2 and by["TEST YOGHURT"].unit_price == 1.25 and by["TEST YOGHURT"].is_bonus
    # the name line ends in "0,50": it is still the name of the "2 X 0,79" line below it
    assert by["TEST WATER 0,50"].quantity == 2 and by["TEST WATER 0,50"].amount == 1.58
    assert not any(n.startswith("2 X") for n in by)
    for not_a_product in ("STATIEGELD", "NL INCL.HEF.SUP 0,01", "PAPIEREN TAS", "JUMBO LUSDRAAGTAS"):
        assert by[not_a_product].product_key is None
    assert by["TEST KOFFIEBONEN"].product_key == "name:TEST KOFFIEBONEN"


def test_unpadded_cells_keep_their_own_names() -> None:
    case = LAYOUTS["unpadded_cells"]
    p = parse_receipt(case["summary"], case["detail"])
    assert [ln.name for ln in p.lines] == ["TEST BROOD WIT", "TEST BOTER", "TEST EIEREN 10ST"]
    assert [ln.is_bonus for ln in p.lines] == [False, True, False]


def test_wrapped_name_and_returned_bottles() -> None:
    case = LAYOUTS["wrapped_and_split_payment"]
    lines = parse_receipt(case["summary"], case["detail"]).lines
    assert lines[0].name == "TEST BIOLOGISCHE GRIEKSE YOGHURT VOLLE 1KG"
    assert lines[2].name == "TEST WASMIDDEL" and lines[2].quantity == 3
    returned = [ln for ln in lines if ln.amount < 0]
    assert [(ln.name, ln.amount, ln.product_key) for ln in returned] == [("STATIEGELD RETOUR", -0.5, None)]


def test_text_with_newlines_becomes_several_lines() -> None:
    doc = {"documents": [{"printSections": [{"textObjects": [{"textLines": [{"texts": [{"text": "A\nB\n\nC"}]}]}]}]}]}
    assert receipt_text_lines(doc) == ["A", "B", "C"]


HITS = [
    {"title": "Voorbeeldmerk Kiwi Goud 3 Stuks", "sku": "1PAK"},  # promoted: comes first for every search
    {"title": "Voorbeeld Bosbessen Vers 125 g", "sku": "2PAK"},
    {"title": "Voorbeeld Halfvolle Melk 1 L", "sku": "3PAK"},
]


@pytest.mark.parametrize(
    ("name", "sku"),
    [
        ("BOSBESSEN VERS", "2PAK"),
        ("VOORB HV MELK", "3PAK"),
        ("HALFVOLLE MELK 1L", "3PAK"),
        ("TEST LIMOENEN", None),
        ("2 X 0,00", None),
    ],
)
def test_best_hit_ignores_unrelated_first_hit(name: str, sku: str | None) -> None:
    hit = best_hit(name, HITS)
    assert (hit or {}).get("sku") == sku


def test_title_matches_abbreviations() -> None:
    assert title_matches("TEST KRENTENBOL", "Test Krentenbollen 6 Stuks")
    assert title_matches("PANBROOD VOLK H", "Voorbeeld Panbrood Volkoren Half")
    assert title_matches("MINIPAPRIKA", "Paprika Mini Mix")
    assert not title_matches("LIMOENEN", "Voorbeeldmerk Kiwi Goud 3 Stuks")
    assert not title_matches("TEST PUDDING VANILLE", "Voorbeeld Vanille Vla")


def test_lookup_never_gives_every_product_the_same_title(config: Config) -> None:
    """A search that returns the same promoted product for every query must not become every title."""

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"searchProducts": {"products": HITS[:1]}}})

    j = Jumbo(config, client=httpx.Client(transport=httpx.MockTransport(handler)))
    j.tokens.save({"access_token": "A", "refresh_token": "R", "expires_in": 86400})
    infos = list(j.lookup_products([("name:A", "BOSBESSEN VERS"), ("name:B", "TEST LIMOENEN"), ("name:C", "MELK")]))
    assert [i.lookup_state for i in infos] == ["missing"] * 3
    assert {i.title for i in infos} == {None}


def test_archived_raw_reparses(config: Config) -> None:
    """What fetch_purchase archives is exactly what parse_raw needs."""
    case = LAYOUTS["padded_mixed"]
    raw = json.loads(json.dumps({"summary": case["summary"], "detail": case["detail"]}))
    assert Jumbo(config).parse_raw("store", raw).total == case["total"]
