from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from groceries import connect as connect_mod
from groceries.config import Config
from groceries.connect import ConnectError, Connections, parse_pasted
from groceries.providers.base import AuthError
from groceries.web import create_app
from groceries.web_connect import bookmarklet

ORIGIN = {"Origin": "http://testserver"}


class FakeLoginProvider:
    """Stands in for a provider's web login; records what was exchanged."""

    exchanged: list[tuple[dict, str]] = []

    def __init__(self, slug: str, config: Config) -> None:
        self.slug, self.config = slug, config

    def begin_login(self, state: str) -> tuple[str, dict]:
        return f"https://login.example.invalid/authorize?state={state}", {"verifier": "v-" + self.slug}

    def complete_login(self, pending: dict, code: str) -> str | None:
        if code == "bad":
            raise AuthError("rejected")
        FakeLoginProvider.exchanged.append((pending, code))
        (self.config.secrets_dir / f"{self.slug}-token.json").write_text(json.dumps({"access_token": "x"}))
        return "u***@example.invalid"


@pytest.fixture
def client(config: Config, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    FakeLoginProvider.exchanged = []
    monkeypatch.setattr(connect_mod, "get_provider", lambda slug, cfg: FakeLoginProvider(slug, cfg))
    return TestClient(create_app(config))


def _start(client: TestClient, slug: str = "jumbo") -> str:
    r = client.post(f"/api/connections/{slug}/start", json={}, headers=ORIGIN)
    assert r.status_code == 200
    assert "gc_login_" + slug in r.cookies
    return r.json()["url"].split("state=")[1]


def test_full_connect_flow(client: TestClient, config: Config) -> None:
    state = _start(client)
    r = client.post("/api/connections/jumbo/finish", json={"code": "C1", "state": state}, headers=ORIGIN)
    assert r.status_code == 200 and r.json() == {"connected": True, "account": "u***@example.invalid"}
    assert FakeLoginProvider.exchanged == [
        ({"state": state, "created": pytest.approx(time.time(), abs=5), "verifier": "v-jumbo"}, "C1")
    ]
    assert not (config.secrets_dir / "jumbo-weblogin.json").exists()  # single use
    status = {s["retailer"]: s for s in client.get("/api/connections").json()["retailers"]}
    assert status["jumbo"]["connected"] and status["jumbo"]["account"] == "u***@example.invalid"
    assert not status["ah"]["connected"]
    # nothing secret in the status payload
    assert "verifier" not in json.dumps(status) and "access_token" not in json.dumps(status)


def test_finish_needs_the_login_cookie(client: TestClient) -> None:
    _start(client)
    client.cookies.clear()  # e.g. a forged request from another browser
    r = client.post("/api/connections/jumbo/finish", json={"code": "C1"}, headers=ORIGIN)
    assert r.status_code == 400 and "another browser" in r.json()["detail"]
    assert FakeLoginProvider.exchanged == []


def test_finish_rejects_foreign_oauth_state(client: TestClient) -> None:
    _start(client)
    r = client.post("/api/connections/jumbo/finish", json={"code": "C1", "state": "someone-else"}, headers=ORIGIN)
    assert r.status_code == 400 and "different login attempt" in r.json()["detail"]


def test_cross_origin_posts_are_refused(client: TestClient) -> None:
    for path in ("/api/connections/ah/start", "/api/connections/ah/disconnect", "/api/connections/ah/sync"):
        assert client.post(path, json={}, headers={"Origin": "https://evil.example"}).status_code == 403
        assert client.post(path, json={}).status_code == 403  # no Origin/Referer at all
    r = client.post("/api/connections/ah/start", data="x", headers={**ORIGIN, "Content-Type": "text/plain"})
    assert r.status_code == 415


def test_expired_login(client: TestClient, config: Config) -> None:
    _start(client)
    path = config.secrets_dir / "jumbo-weblogin.json"
    pending = json.loads(path.read_text())
    pending["created"] -= 16 * 60
    path.write_text(json.dumps(pending))
    r = client.post("/api/connections/jumbo/finish", json={"code": "C1"}, headers=ORIGIN)
    assert r.status_code == 400 and "15 minutes" in r.json()["detail"]


def test_rejected_code_is_reported_and_pending_cleared(client: TestClient, config: Config) -> None:
    _start(client, "ah")
    r = client.post("/api/connections/ah/finish", json={"pasted": "appie://login-exit?code=bad"}, headers=ORIGIN)
    assert r.status_code == 400 and r.json()["detail"] == "rejected"
    assert not (config.secrets_dir / "ah-weblogin.json").exists()


def test_disconnect_deletes_tokens(client: TestClient, config: Config) -> None:
    state = _start(client)
    client.post("/api/connections/jumbo/finish", json={"code": "C1", "state": state}, headers=ORIGIN)
    assert client.post("/api/connections/jumbo/disconnect", json={}, headers=ORIGIN).status_code == 200
    assert not (config.secrets_dir / "jumbo-token.json").exists()
    status = {s["retailer"]: s for s in client.get("/api/connections").json()["retailers"]}
    assert not status["jumbo"]["connected"] and status["jumbo"]["account"] is None


def test_helper_puts_the_code_in_the_fragment() -> None:
    from urllib.parse import unquote

    js = unquote(bookmarklet("http://192.0.2.1:8000"))
    assert '"#code=" + encodeURIComponent(code)' in js and "&code=" not in js


def test_callback_page_asks_before_connecting(client: TestClient) -> None:
    _start(client, "ah")
    r = client.get("/connect/callback", params={"provider": "ah", "code": "C1"})
    assert r.status_code == 200 and "Connect Albert Heijn?" in r.text and 'id="go"' in r.text
    assert "location.hash" in r.text and "C1" not in r.text  # the code is read from #fragment only
    assert FakeLoginProvider.exchanged == []  # nothing is exchanged until the user confirms
    assert client.get("/connect/callback", params={"provider": "nope"}).status_code == 404


def test_finish_rejects_malformed_bodies(client: TestClient) -> None:
    _start(client)
    for body in (b"not json", b"[1, 2]"):
        r = client.post(
            "/api/connections/jumbo/finish", content=body, headers={**ORIGIN, "Content-Type": "application/json"}
        )
        assert r.status_code == 400


def test_needs_reconnect_after_auth_failure(client: TestClient, config: Config) -> None:
    from groceries.db import connect as db_connect

    state = _start(client, "ah")
    client.post("/api/connections/ah/finish", json={"code": "C1", "state": state}, headers=ORIGIN)
    conn = db_connect(config.db_path)
    with conn:
        conn.execute(
            "INSERT INTO sync_runs (retailer, started_at, status, message) "
            "VALUES ('ah', '2026-01-01T00:00:00', 'error', 'login needed: AH token endpoint said 401')"
        )
    status = {s["retailer"]: s for s in client.get("/api/connections").json()["retailers"]}
    assert status["ah"]["needs_reconnect"]


@pytest.mark.parametrize(
    "pasted,expected",
    [
        ("appie://login-exit?code=abc", ("abc", None)),
        ("jumbo://callbacks/login?code=abc&state=s1", ("abc", "s1")),
        (
            '<form method="post" action="jumbo://callbacks/login"><input type="hidden" name="code" value="abc"/>'
            '<input type="hidden" name="state" value="s1"/></form>',
            ("abc", "s1"),
        ),
        ("abc", ("abc", None)),
        (
            "Failed to launch 'jumbo://callbacks/login?code=abc&state=s1' because the scheme does not have a "
            "registered handler.",
            ("abc", "s1"),
        ),
        (
            "Failed to launch 'appie://login-exit?code=abc' because the scheme does not have a registered handler.",
            ("abc", None),
        ),
    ],
)
def test_parse_pasted(pasted: str, expected: tuple) -> None:
    assert parse_pasted(pasted) == expected


def test_bookmarklet_is_one_encoded_url() -> None:
    js = bookmarklet("http://192.0.2.1:8000")
    assert js.startswith("javascript:") and "\n" not in js and "#" not in js
    assert "192.0.2.1" in js


def test_unknown_retailer(config: Config) -> None:
    with pytest.raises(ConnectError):
        Connections(config).start("nope")


def test_proxy_auth_header(config: Config) -> None:
    config.auth_proxy_header = "Remote-User"
    config.trusted_proxies = "192.0.2.10"
    via_proxy = TestClient(create_app(config), client=("192.0.2.10", 40000))
    assert via_proxy.get("/api/connections").status_code == 403
    assert via_proxy.get("/healthz").status_code == 200
    assert via_proxy.get("/api/connections", headers={"Remote-User": "me"}).status_code == 200
    # a forwarded client address must not hide that the request came through the proxy
    fwd = {"Remote-User": "me", "X-Forwarded-For": "198.51.100.7"}
    assert via_proxy.get("/api/connections", headers=fwd).status_code == 200


def test_proxy_auth_header_is_ignored_from_other_peers(config: Config) -> None:
    config.auth_proxy_header = "Remote-User"
    config.trusted_proxies = "192.0.2.10"
    direct = TestClient(create_app(config), client=("198.51.100.7", 40000))
    assert direct.get("/api/connections", headers={"Remote-User": "me"}).status_code == 403


def test_proxy_auth_header_needs_trusted_proxies(config: Config) -> None:
    config.auth_proxy_header = "Remote-User"
    with pytest.raises(ValueError):
        create_app(config)


def test_malformed_host_is_refused(client: TestClient) -> None:
    for host in ("[::1]evil", 'localhost:80";alert(1);//', "localhost:abc"):
        assert client.get("/", headers={"Host": host}).status_code == 421


def test_odd_state_values_are_a_clean_error(client: TestClient) -> None:
    for state in ("\u00e9", ["x"], 12345):
        _start(client)
        r = client.post("/api/connections/jumbo/finish", json={"code": "C1", "state": state}, headers=ORIGIN)
        assert r.status_code == 400 and "different login attempt" in r.json()["detail"]


def test_helper_verifier_overrides_pending(client: TestClient) -> None:
    _start(client)
    verifier = "h" * 64
    r = client.post("/api/connections/jumbo/finish", json={"code": "C2", "verifier": verifier}, headers=ORIGIN)
    assert r.status_code == 200
    assert FakeLoginProvider.exchanged[-1][0]["verifier"] == verifier


def test_helper_verifier_still_needs_the_cookie(client: TestClient) -> None:
    _start(client)
    client.cookies.clear()
    r = client.post("/api/connections/jumbo/finish", json={"code": "C2", "verifier": "h" * 64}, headers=ORIGIN)
    assert r.status_code == 400 and FakeLoginProvider.exchanged == []


def test_bad_verifier_rejected(client: TestClient) -> None:
    _start(client)
    r = client.post("/api/connections/jumbo/finish", json={"code": "C2", "verifier": "short"}, headers=ORIGIN)
    assert r.status_code == 400 and "verifier" in r.json()["detail"]


def test_bookmarklet_has_jumbo_client_filled_in() -> None:
    js = bookmarklet("http://192.0.2.1:8000")
    assert "__JUMBO" not in js and "JxF3wi1b7MOTd9qXutaxqd9uHt4KmG3U" in js


def test_unknown_host_names_are_refused(config: Config) -> None:
    """DNS rebinding: a request for someone else's hostname must not be served."""
    c = TestClient(create_app(config))
    assert c.get("/api/connections", headers={"Host": "evil.example"}).status_code == 421
    assert c.get("/api/connections", headers={"Host": "192.0.2.10:8000"}).status_code == 200
    assert c.get("/api/connections", headers={"Host": "localhost:8000"}).status_code == 200
    config.allowed_hosts = "groceries.home.example"
    c = TestClient(create_app(config))
    assert c.get("/api/connections", headers={"Host": "groceries.home.example"}).status_code == 200


def test_overlong_paste_is_refused() -> None:
    with pytest.raises(ConnectError):
        parse_pasted("name='code' " * 10_000)


def test_overlong_paste_is_a_clean_error(client: TestClient) -> None:
    _start(client)
    r = client.post("/api/connections/jumbo/finish", json={"pasted": "x" * 40_000}, headers=ORIGIN)
    assert r.status_code == 400 and "too long" in r.json()["detail"]
