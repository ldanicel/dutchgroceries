from __future__ import annotations

import json
import stat
import time

import httpx
import pytest

from groceries.config import Config
from groceries.providers.ah import AlbertHeijn, TokenStore
from groceries.providers.base import AuthError


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_refresh_rotates_and_persists(config: Config) -> None:
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        calls.append(body["refreshToken"])
        return httpx.Response(200, json={"access_token": "A2", "refresh_token": "R2", "expires_in": 3600})

    store = TokenStore(config.secrets_dir, _client(handler))
    store.save({"access_token": "A1", "refresh_token": "R1", "expires_in": 0})
    assert store.access_token() == "A2"
    saved = json.loads(store.path.read_text())
    assert saved["refresh_token"] == "R2" and calls == ["R1"]
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(config.secrets_dir.stat().st_mode) == 0o700
    # still valid: no second refresh
    assert store.access_token() == "A2" and calls == ["R1"]


def test_rejected_refresh_is_auth_error(config: Config) -> None:
    store = TokenStore(config.secrets_dir, _client(lambda req: httpx.Response(401, json={})))
    store.save({"access_token": "A1", "refresh_token": "R1", "expires_in": 0})
    with pytest.raises(AuthError):
        store.access_token()
    assert json.loads(store.path.read_text())["refresh_token"] == "R1"  # old token kept


def test_missing_token_is_auth_error(config: Config) -> None:
    with pytest.raises(AuthError, match="groceries login ah"):
        TokenStore(config.secrets_dir, _client(lambda r: httpx.Response(500))).access_token()


def test_api_401_forces_one_refresh(config: Config) -> None:
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/token/refresh"):
            return httpx.Response(200, json={"access_token": "fresh", "refresh_token": "R2", "expires_in": 3600})
        seen.append(req.headers["authorization"])
        if req.headers["authorization"] == "Bearer stale":
            return httpx.Response(401)
        return httpx.Response(200, json={"data": {"ok": 1}})

    ah = AlbertHeijn(config, client=_client(handler))
    ah.tokens.save({"access_token": "stale", "refresh_token": "R1", "expires_in": 3600})
    assert ah.graphql("{ ok }") == {"ok": 1}
    assert seen == ["Bearer stale", "Bearer fresh"]
    assert json.loads(ah.tokens.path.read_text())["obtained_at"] <= time.time()
