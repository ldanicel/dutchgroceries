from __future__ import annotations

import logging
import sqlite3
import threading
import time

import pytest
from fastapi.testclient import TestClient

from groceries import __version__, accounts, reimport, sync, upgrade
from groceries.cli import main
from groceries.config import Config, env_flag
from groceries.db import connect
from groceries.upgrade import (
    auto_reimport,
    run_on_start,
    start_in_background,
    store_version,
    stored_version,
)
from groceries.web import create_app

from .conftest import FakeProvider
from .test_reimport import _jumbo_raw, _store_buggy


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Counts the reimport runs that auto_reimport starts."""
    seen: list[int] = []
    real = reimport.reimport

    def counting(*args, **kwargs):  # type: ignore[no-untyped-def]
        seen.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(upgrade, "reimport", counting)
    return seen


def test_version_bump_reimports_exactly_once(config: Config, calls: list[int]) -> None:
    conn, pid, _ = _store_buggy(config)  # stored by an older parser, before the meta table existed
    assert stored_version(conn) is None
    res = auto_reimport(conn, config, "1.2.1")
    assert res is not None and len(res.changed) == 1 and calls == [1]
    assert conn.execute("SELECT total FROM purchases WHERE id = ?", (pid,)).fetchone()[0] == 21.38
    assert stored_version(conn) == "1.2.1"
    # every later start of the same version: nothing runs
    assert auto_reimport(conn, config, "1.2.1") is None
    assert auto_reimport(connect(config.db_path), config, "1.2.1") is None
    assert calls == [1]
    # the next upgrade runs it once more and finds nothing left to change
    res = auto_reimport(conn, config, "1.3.0")
    assert res is not None and res.changed == [] and calls == [1, 1]
    assert auto_reimport(conn, config, "1.3.0") is None and calls == [1, 1]


def test_fresh_database_only_records_the_version(config: Config, calls: list[int]) -> None:
    conn = connect(config.db_path)
    assert auto_reimport(conn, config, "1.2.1") is None  # no purchases: only the version is recorded
    assert stored_version(conn) == "1.2.1" and calls == []


def test_downgrade_and_upgrade_again_reimport_each_time(config: Config, calls: list[int]) -> None:
    conn, _pid, _ = _store_buggy(config)
    store_version(conn, "1.3.0")
    assert auto_reimport(conn, config, "1.2.1") is not None  # the older parser re-parses what 1.3.0 stored
    assert stored_version(conn) == "1.2.1"
    assert auto_reimport(conn, config, "1.3.0") is not None  # and the upgrade back re-parses once more
    assert auto_reimport(conn, config, "1.3.0") is None
    assert stored_version(conn) == "1.3.0" and calls == [1, 1]


def test_disabled_by_config(config: Config, calls: list[int]) -> None:
    conn, _pid, _ = _store_buggy(config)
    config.auto_reimport = False
    assert auto_reimport(conn, config, "1.2.1") is None
    assert stored_version(conn) is None and calls == []  # runs later if it is turned back on


def test_failure_does_not_block_and_is_retried(
    config: Config, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    conn, _pid, _ = _store_buggy(config)

    def broken(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(upgrade, "reimport", broken)
    with caplog.at_level(logging.WARNING):
        run_on_start(config)  # does not raise
    assert "automatic reimport after the upgrade failed" in caplog.text
    assert stored_version(conn) is None  # so the next start tries again
    monkeypatch.undo()
    run_on_start(config)
    assert stored_version(conn) == __version__


def _db_is_writable(config: Config) -> bool:
    other = sqlite3.connect(config.db_path, timeout=0.5)
    try:
        other.execute("BEGIN IMMEDIATE")
        other.rollback()
        return True
    except sqlite3.OperationalError:
        return False
    finally:
        other.close()


def test_failure_inside_a_write_leaves_the_database_unlocked(
    config: Config, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    conn, _pid, account = _store_buggy(config)

    def half_done(conn: sqlite3.Connection, *args, **kwargs):  # type: ignore[no-untyped-def]
        conn.execute("UPDATE purchases SET total = 0")  # opens a write transaction...
        raise sqlite3.IntegrityError("UNIQUE constraint failed: accounts.retailer, accounts.label")  # ...and fails

    monkeypatch.setattr(upgrade, "reimport", half_done)
    with caplog.at_level(logging.WARNING):
        run_on_start(config)
    assert "automatic reimport after the upgrade failed" in caplog.text
    assert _db_is_writable(config)
    assert conn.execute("SELECT MIN(total) FROM purchases").fetchone()[0] == 21.88  # rolled back
    assert stored_version(conn) is None


def test_orphan_raw_file_is_skipped_and_sync_still_works(
    config: Config, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A raw file without a purchase (`accounts remove --purge` of an older version, or a sync that saved
    the file but failed to store it) used to make the upgrade create a second "Main" account; the
    IntegrityError left the database locked and every sync failed with "database is locked"."""
    conn, pid, account = _store_buggy(config)
    orphan, _rel = _jumbo_raw(config, "no_header_cash")
    accounts.create(conn, "ah")
    monkeypatch.setattr(sync, "get_provider", lambda slug, cfg, account=None: FakeProvider(cfg))
    with caplog.at_level(logging.INFO):
        run_on_start(config)
    assert "1 raw files without a stored purchase skipped" in caplog.text
    assert stored_version(conn) == __version__
    assert conn.execute("SELECT total FROM purchases WHERE id = ?", (pid,)).fetchone()[0] == 21.38
    assert conn.execute("SELECT COUNT(*) FROM purchases WHERE retailer = 'jumbo'").fetchone()[0] == 1  # not back
    assert [(a["retailer"], a["label"]) for a in accounts.list_accounts(conn, config)] == [
        ("ah", "Main"),
        ("jumbo", "Main"),
    ]
    assert _db_is_writable(config)
    res = sync.run_sync(config, "ah")
    assert (res.fetched, res.failed) == (2, 0)


def test_manual_reimport_of_an_orphan_uses_the_existing_account(config: Config) -> None:
    conn, _pid, account = _store_buggy(config)
    _jumbo_raw(config, "no_header_cash")
    res = reimport.reimport(conn, config, ["jumbo"])
    assert res.failed == 0 and sorted(d.new for d in res.changed) == [False, True]
    assert {r[0] for r in conn.execute("SELECT account_id FROM purchases")} == {account}
    assert conn.execute("SELECT COUNT(*) FROM accounts WHERE retailer = 'jumbo'").fetchone()[0] == 1


def test_store_error_mid_run_is_counted_and_the_rest_continues(config: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    conn, pid, _ = _store_buggy(config)
    _jumbo_raw(config, "no_header_cash")
    real, seen = reimport.store_purchase, []

    def flaky(conn: sqlite3.Connection, p, *args, **kwargs):  # type: ignore[no-untyped-def]
        seen.append(p.id)
        real(conn, p, *args, **kwargs)
        if len(seen) == 1:
            raise sqlite3.IntegrityError("simulated")

    monkeypatch.setattr(reimport, "store_purchase", flaky)
    res = reimport.reimport(conn, config, ["jumbo"])
    assert res.failed == 1 and len(res.changed) == 1 and len(seen) == 2
    assert not conn.in_transaction and _db_is_writable(config)
    # the failed purchase is rolled back to what it was before: still the buggy total, or not stored at all
    before = {pid: 21.88}
    assert [tuple(r) for r in conn.execute("SELECT total FROM purchases WHERE id = ?", (seen[0],))] == (
        [(before[seen[0]],)] if seen[0] in before else []
    )


def test_purge_deletes_the_raw_files(config: Config, calls: list[int]) -> None:
    conn, _pid, account = _store_buggy(config)
    raw = config.raw_dir / "jumbo" / "store"
    assert len(list(raw.glob("*.json"))) == 1
    accounts.remove(conn, config, account, purge=True)
    assert list(raw.glob("*.json")) == []
    assert reimport.reimport(conn, config, ["jumbo"]).files == 0  # nothing left to bring back


def test_purge_never_deletes_outside_raw(config: Config, tmp_path) -> None:  # type: ignore[no-untyped-def]
    conn, _pid, account = _store_buggy(config)
    outside = tmp_path / "keep.json"
    outside.write_text("{}")
    link = config.raw_dir / "jumbo" / "store" / "link.json"
    link.symlink_to(outside)
    with conn:
        for i, rel in enumerate(["../keep.json", str(outside), str(link.relative_to(config.data_dir))]):
            conn.execute(
                "INSERT INTO purchases (id, retailer, channel, external_id, ts_utc, local_date, total, raw_file, "
                "account_id) VALUES (?, 'jumbo', 'store', ?, '2026-01-01T00:00:00Z', '2026-01-01', 1, ?, ?)",
                (f"jumbo:store:x{i}", f"x{i}", rel, account),
            )
    accounts.remove(conn, config, account, purge=True)
    assert outside.read_text() == "{}" and link.is_symlink()


def test_store_failure_is_retried_on_the_next_start(config: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    conn, pid, _ = _store_buggy(config)

    def locked(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(reimport, "store_purchase", locked)
    run_on_start(config)
    assert stored_version(conn) is None
    monkeypatch.undo()
    run_on_start(config)
    assert stored_version(conn) == __version__
    assert conn.execute("SELECT total FROM purchases WHERE id = ?", (pid,)).fetchone()[0] == 21.38


@pytest.mark.parametrize(
    ("value", "expected", "warns"),
    [
        (None, True, False),
        ("", True, False),
        ("false", False, False),
        (" FALSE ", False, False),
        ('"false"', False, False),
        ("'Off'", False, False),
        ("0", False, False),
        ("no", False, False),
        ("N", False, False),
        ("disabled", False, False),
        ("true", True, False),
        ("yes", True, False),
        ("flase", True, True),
    ],
)
def test_auto_reimport_flag(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, value: str | None, expected: bool, warns: bool
) -> None:
    if value is None:
        monkeypatch.delenv("GROCERIES_AUTO_REIMPORT", raising=False)
    else:
        monkeypatch.setenv("GROCERIES_AUTO_REIMPORT", value)
    with caplog.at_level(logging.WARNING):
        assert env_flag("GROCERIES_AUTO_REIMPORT", True) is expected
    assert ("not a yes/no value" in caplog.text) is warns


def test_serve_and_sync_starting_together_reimport_once(
    config: Config, monkeypatch: pytest.MonkeyPatch, calls: list[int]
) -> None:
    _store_buggy(config)
    real = upgrade.reimport

    def slow(*args, **kwargs):  # type: ignore[no-untyped-def]
        time.sleep(0.3)  # long enough for the other start to reach the lock
        return real(*args, **kwargs)

    monkeypatch.setattr(upgrade, "reimport", slow)
    results: list[object] = []
    starts = [
        threading.Thread(target=lambda: results.append(auto_reimport(connect(config.db_path), config, "1.2.1")))
        for _ in range(2)
    ]
    for t in starts:
        t.start()
    for t in starts:
        t.join(10)
    assert calls == [1] and sorted(r is None for r in results) == [False, True]


def test_serve_runs_it_in_the_background(config: Config, calls: list[int]) -> None:
    _store_buggy(config)
    start_in_background(config).join(10)
    assert calls == [1] and stored_version(connect(config.db_path)) == __version__


def test_sync_command_runs_it_once(
    config: Config, monkeypatch: pytest.MonkeyPatch, calls: list[int], caplog: pytest.LogCaptureFixture
) -> None:
    conn, pid, _ = _store_buggy(config)
    store_version(conn, "1.0.0")
    monkeypatch.setattr(Config, "from_env", classmethod(lambda cls: config))
    with caplog.at_level(logging.INFO):
        assert main(["sync", "--account", "Main", "jumbo"]) == 1  # no login stored: the sync itself fails
    assert calls == [1] and stored_version(conn) == __version__
    assert "version changed from 1.0.0 to" in caplog.text and f"{pid}: total 21.88 -> 21.38" in caplog.text
    main(["sync", "--account", "Main", "jumbo"])
    assert calls == [1]


def test_version_shown(config: Config, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as e:
        main(["--version"])
    assert e.value.code == 0 and capsys.readouterr().out.strip() == f"dutchgroceries {__version__}"
    client = TestClient(create_app(config))
    assert client.get("/healthz").json() == {"status": "ok", "version": __version__}
    page = client.get("/").text
    assert f"dutchgroceries {__version__}</a></footer>" in page and "{{VERSION}}" not in page
