from __future__ import annotations

import json
import sqlite3

import pytest

from groceries import sync
from groceries.cli import main
from groceries.config import Config
from groceries.db import connect, main_account
from groceries.providers.base import Line
from groceries.providers.jumbo import parse_receipt
from groceries.reimport import redact, reimport
from groceries.storage import write_json_atomic
from groceries.sync import store_purchase

from .conftest import FakeProvider, load


def _jumbo_raw(config: Config, name: str = "padded_mixed") -> tuple[dict, str]:
    case = load("jumbo_receipt_layouts.json")[name]
    raw = {"summary": case["summary"], "detail": case["detail"]}
    path = config.raw_dir / "jumbo" / "store" / f"{sync.safe_name(case['summary']['transactionId'])}.json"
    write_json_atomic(path, raw)
    return raw, str(path.relative_to(config.data_dir))


def _store_buggy(config: Config) -> tuple[sqlite3.Connection, str, int]:
    """A receipt as an older parser stored it: a "2 X 0,79" line named after itself, and a search title."""
    raw, rel = _jumbo_raw(config)
    conn = connect(config.db_path)
    p = parse_receipt(raw["summary"], raw["detail"])
    i = next(n for n, ln in enumerate(p.lines) if ln.name == "TEST WATER 0,50")
    p.lines[i : i + 1] = [
        Line(name="TEST WATER", amount=0.50, unit_price=0.50, product_key="name:TEST WATER"),
        Line(name="2 X 0,79", amount=1.58, unit_price=1.58, product_key="name:2 X 0,79"),
    ]
    p.total = 21.88
    account = main_account(conn, "jumbo")
    with conn:
        store_purchase(conn, p, config.timezone, rel, account)
        conn.execute(
            "UPDATE products SET title = 'Voorbeeldmerk Kiwi Goud', webshop_id = '1PAK', lookup_state = 'ok' "
            "WHERE product_key = 'name:TEST APPELS'"
        )
    return conn, p.id, account


def test_dry_run_reports_and_writes_nothing(config: Config) -> None:
    conn, pid, _ = _store_buggy(config)
    res = reimport(conn, config, ["jumbo"], dry_run=True)
    s = res.summary()
    assert (s["receipts_changed"], s["lines_added"], s["lines_removed"]) == (1, 1, 2)
    assert (s["total_before"], s["total_after"]) == (21.88, 21.38)
    assert res.titles_dropped == [("jumbo", "TEST APPELS", "Voorbeeldmerk Kiwi Goud")]
    assert conn.execute("SELECT total FROM purchases WHERE id = ?", (pid,)).fetchone()[0] == 21.88


def test_apply_is_idempotent(config: Config) -> None:
    conn, pid, account = _store_buggy(config)
    res = reimport(conn, config, ["jumbo"])
    assert len(res.changed) == 1 and res.products_pruned == 2  # "TEST WATER" and "2 X 0,79"
    names = [r[0] for r in conn.execute("SELECT name FROM purchase_lines WHERE purchase_id = ?", (pid,))]
    assert "TEST WATER 0,50" in names and "2 X 0,79" not in names
    row = conn.execute("SELECT total, account_id FROM purchases WHERE id = ?", (pid,)).fetchone()
    assert tuple(row) == (21.38, account)
    prod = conn.execute("SELECT title, lookup_state FROM products WHERE product_key = 'name:TEST APPELS'").fetchone()
    assert tuple(prod) == (None, None)  # looked up again on the next sync
    again = reimport(conn, config, ["jumbo"])
    assert again.summary()["receipts_changed"] == 0 and not again.titles_dropped


def test_filters_and_orphan_raw_file(config: Config) -> None:
    conn, pid, _ = _store_buggy(config)
    raw, _rel = _jumbo_raw(config, "no_header_cash")  # e.g. a file shared in an issue: not in the DB yet
    only = reimport(conn, config, ["jumbo"], receipt=raw["summary"]["transactionId"], dry_run=True)
    assert [d.new for d in only.changed] == [True]
    reimport(conn, config, ["jumbo"], account="Main")
    assert conn.execute("SELECT COUNT(*) FROM purchases WHERE retailer = 'jumbo'").fetchone()[0] == 2


def test_ah_raw_roundtrip(config: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    """AH archives its responses the same way: a fresh sync re-imports with no changes."""
    monkeypatch.setattr(sync, "get_provider", lambda slug, cfg, account=None: FakeProvider(cfg))
    sync.run_sync(config, "ah")
    res = reimport(connect(config.db_path), config, ["ah"])
    assert (res.files, res.unchanged, len(res.changed), res.failed) == (2, 2, 0, 0)


def test_cli_dry_run(config: Config, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    _store_buggy(config)
    monkeypatch.setattr(Config, "from_env", classmethod(lambda cls: config))
    assert main(["reimport", "--provider", "jumbo", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "total 21.88 -> 21.38" in out and "- 2 X 0,79 1.58" in out and '"dry_run": true' in out


def test_export_raw_redacts(
    config: Config, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    raw = load("jumbo_receipt.json")
    path = config.raw_dir / "jumbo" / "store" / "a1b2c3-receipt.json"
    write_json_atomic(path, raw)
    monkeypatch.setattr(Config, "from_env", classmethod(lambda cls: config))
    assert main(["export-raw", "--receipt", "a1b2c3-receipt", "--redact"]) == 0
    text = capsys.readouterr().out
    for secret in ("Voorbeeldstraat", "1234AB", "9999", "00000000-0000-4000-8000-000000000001", "receiptPoints"):
        assert secret not in text
    shared = json.loads(text)
    # still parses to the same purchase, under a pseudonymous id
    before, after = parse_receipt(raw["summary"], raw["detail"]), parse_receipt(shared["summary"], shared["detail"])
    assert [(ln.name, ln.amount) for ln in after.lines] == [(ln.name, ln.amount) for ln in before.lines]
    assert after.total == before.total and after.external_id.startswith("redacted-")
    assert after.store_street == "REDACTED REDACTED"


def test_redact_ah_keeps_product_ids() -> None:
    raw = load("ah_receipt.json")
    red = redact(raw)
    assert red["summary"]["id"] == red["detail"]["id"] != raw["summary"]["id"]
    assert [p.get("id") for p in red["detail"]["products"]] == [p.get("id") for p in raw["detail"]["products"]]
    assert "REDACTED" in json.dumps(red["detail"].get("address"))
