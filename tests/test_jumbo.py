from __future__ import annotations

import base64
import hashlib
import json
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from groceries.config import Config
from groceries.providers.base import AuthError, PurchaseRef
from groceries.providers.jumbo import (
    REDIRECT_URI,
    Jumbo,
    parse_order,
    parse_receipt,
    receipt_id_candidates,
)

from .conftest import load


def test_receipt_parses_and_reconciles() -> None:
    r = load("jumbo_receipt.json")
    p = parse_receipt(r["summary"], r["detail"])
    assert p.retailer == "jumbo" and p.channel == "store"
    assert p.total == 13.38
    assert round(sum(ln.amount for ln in p.lines) - p.discount_total, 2) == p.total
    assert round(sum(pm.amount for pm in p.payments), 2) == p.total
    assert (p.store_id, p.store_street, p.store_city) == ("9999", "Voorbeeldstraat 1", "Voorbeeldstad")
    by = {ln.name: ln for ln in p.lines}
    assert by["TEST APPELS"].weight == 0.512 and by["TEST APPELS"].unit_price == 3.49
    assert by["TEST YOGHURT"].quantity == 2 and by["TEST YOGHURT"].unit_price == 1.25 and by["TEST YOGHURT"].is_bonus
    assert by["JUMBO HALFVOLLE MELK"].product_key == "name:JUMBO HALFVOLLE MELK"
    assert [d.amount for d in p.discounts] == [0.63]


def test_order_summary() -> None:
    p = parse_order(load("jumbo_order.json"))
    assert (p.channel, p.external_id, p.total, p.status) == ("online", "900000001", 54.32, "DELIVERED")


def test_receipt_id_candidates() -> None:
    assert receipt_id_candidates("abc-00000000-0000-4000-8000-000000000001.json") == [
        "abc-00000000-0000-4000-8000-000000000001.json",
        "abc-00000000-0000-4000-8000-000000000001",
        "00000000-0000-4000-8000-000000000001",
        "abc",
    ]
    assert receipt_id_candidates("plain") == ["plain"]


def _jumbo(config: Config, handler) -> Jumbo:  # type: ignore[no-untyped-def]
    return Jumbo(config, client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_pkce_login_flow(config: Config) -> None:
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen.update(json.loads(req.content))
        return httpx.Response(
            200, json={"access_token": "A", "refresh_token": "R", "id_token": "I", "expires_in": 86400}
        )

    j = _jumbo(config, handler)
    url = j.tokens.start_login()
    q = parse_qs(urlparse(url).query)
    assert url.startswith("https://auth.jumbo.com/authorize?")
    assert q["redirect_uri"] == [REDIRECT_URI] and q["code_challenge_method"] == ["S256"]
    assert "offline_access" in q["scope"][0]
    j.tokens.finish_login(f"jumbo://callbacks/login?code=CODE123&state={q['state'][0]}")
    assert seen["grant_type"] == "authorization_code" and seen["code"] == "CODE123"
    challenge = base64.urlsafe_b64encode(hashlib.sha256(seen["code_verifier"].encode()).digest()).rstrip(b"=").decode()
    assert challenge == q["code_challenge"][0]
    saved = json.loads(j.tokens.path.read_text())
    assert saved["refresh_token"] == "R" and "id_token" not in saved
    assert not j.tokens.pending.exists()


def test_login_rejects_foreign_state(config: Config) -> None:
    j = _jumbo(config, lambda r: httpx.Response(200, json={}))
    j.tokens.start_login()
    with pytest.raises(AuthError, match="state"):
        j.tokens.finish_login("jumbo://callbacks/login?code=X&state=someone-else")


def test_refresh_keeps_old_token_when_not_rotated(config: Config) -> None:
    j = _jumbo(config, lambda r: httpx.Response(200, json={"access_token": "A2", "expires_in": 86400}))
    j.tokens.save({"access_token": "A1", "refresh_token": "R1", "expires_in": 0})
    assert j.tokens.access_token() == "A2"
    assert json.loads(j.tokens.path.read_text())["refresh_token"] == "R1"


def test_list_and_fetch_with_mocked_api(config: Config) -> None:
    receipt = load("jumbo_receipt.json")
    calls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "auth.jumbo.com":
            return httpx.Response(200, json={"access_token": "A2", "refresh_token": "R2", "expires_in": 86400})
        body = json.loads(req.content)
        op = body["query"].split("(")[0].split()[1]
        calls.append(op)
        if op == "GetReceiptOverview":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "receiptOverview": {
                            "totalResults": 1,
                            "pageSize": 25,
                            "currentPage": 0,
                            "receipts": [receipt["summary"]],
                        }
                    }
                },
            )
        if op == "OrdersPageOrders":  # schema drift must not break the sync
            return httpx.Response(400, json={"errors": [{"message": "Cannot query field"}]})
        if op == "GetDigitalReceipt":
            hit = body["variables"]["transactionId"] == "00000000-0000-4000-8000-000000000001"
            return httpx.Response(200, json={"data": {"receipt": receipt["detail"] if hit else None}})
        return httpx.Response(404)

    config.request_delay = 0
    j = _jumbo(config, handler)
    j.tokens.save({"access_token": "A1", "refresh_token": "R1", "expires_in": 86400})
    refs = list(j.list_purchases())
    assert [(r.channel, r.external_id) for r in refs] == [("store", receipt["summary"]["transactionId"])]
    _raw, p = j.fetch_purchase(refs[0])
    assert p.total == 13.38
    assert calls.count("GetDigitalReceipt") == 3  # raw key, stem, then uuid


def test_unauthenticated_graphql_forces_one_refresh(config: Config) -> None:
    tokens_seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "auth.jumbo.com":
            return httpx.Response(200, json={"access_token": "fresh", "refresh_token": "R2", "expires_in": 86400})
        tokens_seen.append(req.headers["authorization"])
        if req.headers["authorization"] == "Bearer stale":
            return httpx.Response(
                200, json={"data": None, "errors": [{"message": "401", "extensions": {"code": "UNAUTHENTICATED"}}]}
            )
        return httpx.Response(200, json={"data": {"receipt": None}})

    config.request_delay = 0
    j = _jumbo(config, handler)
    j.tokens.save({"access_token": "stale", "refresh_token": "R1", "expires_in": 86400})
    assert j.graphql("query X { receipt { __typename } }") == {"receipt": None}
    assert tokens_seen == ["Bearer stale", "Bearer fresh"]
    assert json.loads(j.tokens.path.read_text())["refresh_token"] == "R2"


def test_online_order_fetch(config: Config) -> None:
    j = _jumbo(config, lambda r: httpx.Response(500))
    o = load("jumbo_order.json")
    _raw, p = j.fetch_purchase(PurchaseRef("online", o["id"], summary=o))
    assert p.total == 54.32


def test_local_time_is_stored_as_utc() -> None:
    from groceries.providers.jumbo import _iso_utc

    assert _iso_utc("2025-01-15 12:00:00") == "2025-01-15T11:00:00Z"  # CET
    assert _iso_utc("2025-07-15 12:00:00") == "2025-07-15T10:00:00Z"  # CEST
    assert _iso_utc("2025-07-15T10:00:00Z") == "2025-07-15T10:00:00Z"
