"""Several store accounts per retailer (synthetic data only)."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from groceries import accounts, cli, sync
from groceries import connect as connect_mod
from groceries.analytics import Analytics
from groceries.config import Config
from groceries.db import SCHEMA, connect
from groceries.providers.ah import parse_receipt
from groceries.providers.base import Purchase, PurchaseRef
from groceries.web import create_app

from .conftest import FakeProvider, load

ORIGIN = {"Origin": "http://testserver"}


class AccountProvider(FakeProvider):
    """The fixture receipt for every account, plus one receipt of its own per account."""

    def __init__(self, config: Config, account: object = None) -> None:
        super().__init__(config)
        self.account = account

    def _own(self) -> dict[str, Any]:
        r = json.loads(json.dumps(self.receipt))
        r["summary"]["id"] = f"own-{self.account}"
        return r

    def list_purchases(self) -> Iterator[PurchaseRef]:
        yield from super().list_purchases()
        own = self._own()
        yield PurchaseRef("store", own["summary"]["id"], summary=own["summary"])

    def fetch_purchase(self, ref: PurchaseRef) -> tuple[dict[str, Any], Purchase]:
        if ref.external_id.startswith("own-"):
            own = self._own()
            return own, parse_receipt(own["summary"], own["detail"])
        return super().fetch_purchase(ref)


def _tokens(config: Config, *account_ids: int) -> None:
    config.secrets_dir.mkdir(parents=True, exist_ok=True)
    for i in account_ids:
        accounts.token_path(config, "ah", i).write_text("{}")


def _legacy_db(path: Path) -> None:
    """A database as the single-account version left it: no accounts table, no account_id columns."""
    old = SCHEMA.split("CREATE TABLE IF NOT EXISTS purchases (", 1)[1]
    old = "CREATE TABLE IF NOT EXISTS purchases (" + old
    old = old.replace("    account_id     INTEGER REFERENCES accounts (id),  -- the account that first synced it\n", "")
    old = old.replace("    account_id    INTEGER,\n", "")
    assert "account_id" not in old and "accounts" not in old
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(old)
    for i, (retailer, total) in enumerate([("ah", 10.5), ("ah", 4.25), ("jumbo", 7.0)]):
        conn.execute(
            "INSERT INTO purchases (id, retailer, channel, external_id, ts_utc, local_date, total) "
            "VALUES (?, ?, 'store', ?, '2025-01-01T10:00:00Z', '2025-01-01', ?)",
            (f"{retailer}:store:{i}", retailer, str(i), total),
        )
    conn.execute("INSERT INTO sync_runs (retailer, started_at, status) VALUES ('ah', '2025-01-01T10:00:00', 'ok')")
    conn.commit()
    conn.close()


def test_migration_from_single_account(config: Config) -> None:
    _legacy_db(config.db_path)
    config.secrets_dir.mkdir(parents=True)
    (config.secrets_dir / "ah-token.json").write_text('{"refresh_token": "synthetic"}')
    (config.data_dir / "connections.json").write_text(json.dumps({"ah": {"account": "u***@x", "connected_at": 5}}))

    conn = connect(config.db_path)
    accounts.migrate_legacy(conn, config)
    accs = {a["retailer"]: a for a in accounts.list_accounts(conn, config)}
    assert {r: a["label"] for r, a in accs.items()} == {"ah": "Main", "jumbo": "Main"}
    assert accs["ah"]["connected"] and accs["ah"]["display"] == "u***@x" and accs["ah"]["connected_at"] == 5
    assert not accs["jumbo"]["connected"]
    assert not (config.secrets_dir / "ah-token.json").exists()
    assert json.loads(accounts.token_path(config, "ah", accs["ah"]["id"]).read_text()) == {"refresh_token": "synthetic"}
    rows = conn.execute("SELECT retailer, account_id, COUNT(*), SUM(total) FROM purchases GROUP BY 1, 2").fetchall()
    assert [tuple(r) for r in rows] == [("ah", accs["ah"]["id"], 2, 14.75), ("jumbo", accs["jumbo"]["id"], 1, 7.0)]
    assert conn.execute("SELECT account_id FROM sync_runs").fetchone()[0] == accs["ah"]["id"]
    # idempotent
    accounts.migrate_legacy(connect(config.db_path), config)
    assert len(accounts.list_accounts(connect(config.db_path), config)) == 2


def test_two_accounts_share_one_archive(config: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sync, "get_provider", lambda slug, cfg, account=None: AccountProvider(cfg, account))
    conn = connect(config.db_path)
    main, partner = accounts.create(conn, "ah"), accounts.create(conn, "ah", "Partner")
    _tokens(config, main, partner)
    res = sync.run_sync(config, "ah")
    # Main: shared receipt, shared order, own receipt. Partner: own receipt; its copies of the shared two are skipped
    assert (res.fetched, res.duplicates) == (4, 2)
    owners = dict(conn.execute("SELECT external_id, account_id FROM purchases WHERE channel = 'store'").fetchall())
    shared = load("ah_receipt.json")["summary"]["id"]
    assert owners == {shared: main, f"own-{main}": main, f"own-{partner}": partner}
    total = Analytics(conn).overview()["purchases"]
    assert (
        total
        == Analytics(conn, account=main).overview()["purchases"]
        + Analytics(conn, account=partner).overview()["purchases"]
    )
    assert Analytics(conn, account=partner).overview()["purchases"] == 1
    assert {p["account"] for p in Analytics(conn).purchases()} == {"Main", "Partner"}
    # second run: nothing new, the shared receipt is still not stored twice
    assert sync.run_sync(config, "ah").fetched == 0
    assert conn.execute("SELECT COUNT(*) FROM purchases").fetchone()[0] == 4


def test_one_failing_account_does_not_stop_the_others(config: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[str] = []
    monkeypatch.setattr("groceries.notify.Notifier.send", lambda self, title, *a, **k: sent.append(title) or True)

    def provider(slug: str, cfg: Config, account: object = None) -> AccountProvider:
        p = AccountProvider(cfg, account)
        if account == 1:
            monkeypatch.setattr(p, "check_auth", lambda: (_ for _ in ()).throw(RuntimeError("API down")))
        return p

    monkeypatch.setattr(sync, "get_provider", provider)
    conn = connect(config.db_path)
    accounts.create(conn, "ah")
    accounts.create(conn, "ah", "Partner")
    _tokens(config, 1, 2)
    with pytest.raises(RuntimeError):
        sync.run_sync(config, "ah")
    assert conn.execute("SELECT COUNT(*) FROM purchases WHERE account_id = 2").fetchone()[0] == 3
    assert sent == ["⚠ AH sync failed (Main)"]
    monkeypatch.setattr(sync, "get_provider", lambda slug, cfg, account=None: AccountProvider(cfg, account))
    sync.run_sync(config, "ah")
    assert sent[-1] == "✅ Resolved: AH sync (Main)"


class LoginProvider:
    """Web login stand-in: codes starting with the same letter are the same store account."""

    def __init__(self, slug: str, config: Config, account: object = None) -> None:
        self.slug, self.config, self.account, self.code = slug, config, account, ""

    def begin_login(self, state: str) -> tuple[str, dict]:
        return f"https://login.example.invalid/authorize?state={state}", {}

    def complete_login(self, pending: dict, code: str) -> str:
        self.code = code
        self.config.secrets_dir.mkdir(parents=True, exist_ok=True)
        (self.config.secrets_dir / f"{self.slug}-{self.account}-token.json").write_text(json.dumps({"code": code}))
        return f"{code[0]}***@example.invalid"

    def identity(self) -> str:
        return f"{self.code[0]}@example.invalid"


@pytest.fixture
def web(config: Config, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(connect_mod, "get_provider", lambda slug, cfg, account=None: LoginProvider(slug, cfg, account))
    return TestClient(create_app(config))


def _connect(web: TestClient, code: str, slug: str = "ah", **start: object) -> Any:
    r = web.post(f"/api/connections/{slug}/start", json=start, headers=ORIGIN)
    assert r.status_code == 200, r.text
    return web.post(f"/api/connections/{slug}/finish", json={"code": code}, headers=ORIGIN)


def test_web_add_rename_and_refuse_duplicates(web: TestClient, config: Config) -> None:
    assert _connect(web, "a1").json()["label"] == "Main"
    r = _connect(web, "b1", label="Partner")
    assert r.status_code == 200 and r.json()["label"] == "Partner" and r.json()["account_id"] == 2
    # the same store account again (e.g. still logged in to the first one in this browser)
    r = _connect(web, "a2", label="Third")
    assert r.status_code == 400 and 'already connected as "Main"' in r.json()["detail"]
    assert "private window" in r.json()["detail"]
    assert not list(config.secrets_dir.glob("*staging*"))
    # reconnecting a slot with another store account is refused; with its own account it works
    r = _connect(web, "b2", account_id=1)
    assert r.status_code == 400 and 'already connected as "Partner"' in r.json()["detail"]
    r = _connect(web, "c1", account_id=1)
    assert r.status_code == 400 and "different Albert Heijn account" in r.json()["detail"]
    assert json.loads(accounts.token_path(config, "ah", 1).read_text()) == {"code": "a1"}  # untouched
    assert _connect(web, "a3", account_id=1).json()["account_id"] == 1
    assert json.loads(accounts.token_path(config, "ah", 1).read_text()) == {"code": "a3"}
    # labels
    r = web.post("/api/accounts/2/rename", json={"label": "  Sam  "}, headers=ORIGIN)
    assert r.json() == {"id": 2, "label": "Sam"}
    assert web.post("/api/accounts/2/rename", json={"label": "main"}, headers=ORIGIN).status_code == 400
    assert web.post("/api/accounts/2/rename", json={"label": ""}, headers=ORIGIN).status_code == 400
    assert web.post("/api/accounts/9/rename", json={"label": "x"}, headers=ORIGIN).status_code == 404
    assert web.post("/api/connections/ah/start", json={"label": "Sam"}, headers=ORIGIN).status_code == 400
    assert web.post("/api/connections/ah/start", json={"account_id": "1"}, headers=ORIGIN).status_code == 400
    assert web.post("/api/connections/jumbo/start", json={"account_id": 1}, headers=ORIGIN).status_code == 400
    [ah] = [s for s in web.get("/api/connections").json()["retailers"] if s["retailer"] == "ah"]
    assert [(a["id"], a["label"], a["connected"]) for a in ah["accounts"]] == [(1, "Main", True), (2, "Sam", True)]
    assert [a["label"] for a in web.get("/api/accounts").json()] == ["Main", "Sam"]


def test_web_account_filter(web: TestClient, config: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    _connect(web, "a1")
    _connect(web, "b1", label="Partner")
    monkeypatch.setattr(sync, "get_provider", lambda slug, cfg, account=None: AccountProvider(cfg, account))
    sync.run_sync(config, "ah")
    everyone = web.get("/api/overview").json()["purchases"]
    partner = web.get("/api/overview", params={"account": 2}).json()["purchases"]
    assert (everyone, partner) == (4, 1)
    assert {p["account"] for p in web.get("/api/purchases", params={"account": 2}).json()} == {"Partner"}
    assert sum(m["baskets"] for m in web.get("/api/months", params={"account": 1}).json()) == 3
    for path in ("/api/categories", "/api/products", "/api/stores", "/api/weekdays", "/api/price-index"):
        assert web.get(path, params={"account": 2}).status_code == 200


def test_cli_accounts(config: Config, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("GROCERIES_DATA_DIR", str(config.data_dir))
    monkeypatch.setenv("GROCERIES_SECRETS_DIR", str(config.secrets_dir))
    monkeypatch.setattr(sync, "get_provider", lambda slug, cfg, account=None: AccountProvider(cfg, account))
    conn = connect(config.db_path)
    accounts.create(conn, "ah")
    accounts.create(conn, "ah", "Partner")
    _tokens(config, 1, 2)
    assert cli.main(["sync", "ah", "--account", "partner"]) == 0
    assert [tuple(r) for r in conn.execute("SELECT DISTINCT account_id FROM purchases")] == [(2,)]
    assert cli.main(["accounts", "rename", "Partner", "Sam"]) == 0
    cli.main(["accounts", "list"])
    out = capsys.readouterr().out
    assert "Sam" in out and "3 purchases" in out
    monkeypatch.setattr("builtins.input", lambda prompt: "no")
    assert cli.main(["accounts", "remove", "Sam"]) == 1
    assert accounts.token_path(config, "ah", 2).exists()
    assert cli.main(["accounts", "remove", "Sam", "--yes"]) == 0
    assert not accounts.token_path(config, "ah", 2).exists()
    assert conn.execute("SELECT COUNT(*) FROM purchases WHERE account_id = 2").fetchone()[0] == 3  # data kept
    assert cli.main(["accounts", "remove", "2", "--yes", "--purge"]) == 0
    assert conn.execute("SELECT COUNT(*) FROM purchases").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM purchase_lines").fetchone()[0] == 0
    assert [a["label"] for a in accounts.list_accounts(conn, config)] == ["Main"]


def test_labels_are_validated(config: Config) -> None:
    conn = connect(config.db_path)
    with pytest.raises(accounts.AccountError):
        accounts.create(conn, "ah", "x" * 41)
    accounts.create(conn, "ah", "Main")
    with pytest.raises(accounts.AccountError):
        accounts.create(conn, "ah", "MAIN")
    assert accounts.create(conn, "jumbo", "Main")  # labels are per retailer
    assert accounts.create(conn, "ah") and accounts.get(conn, 3)["label"] == "Account 2"
