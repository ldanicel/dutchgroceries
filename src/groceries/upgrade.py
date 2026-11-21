"""Re-parse the stored purchases once after an upgrade.

The database remembers the app version that last checked it (table ``meta``). When ``sync`` or ``serve``
starts with a different version (an upgrade, or a downgrade and back), the saved raw responses of the
stored purchases are re-imported once, so parser fixes reach purchases synced before the upgrade without a
manual ``groceries reimport``. Raw files without a stored purchase (for example of purchases deleted with
``accounts remove --purge``) are left alone. Turn it off with ``GROCERIES_AUTO_REIMPORT=false``.
"""

from __future__ import annotations

import logging
import sqlite3
import threading

from . import __version__
from .config import Config
from .db import connect
from .reimport import ReimportResult, reimport
from .storage import file_lock

log = logging.getLogger(__name__)

VERSION_KEY = "app_version"


def stored_version(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (VERSION_KEY,)).fetchone()
    return row[0] if row else None


def store_version(conn: sqlite3.Connection, version: str = __version__) -> None:
    with conn:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (VERSION_KEY, version),
        )


def auto_reimport(conn: sqlite3.Connection, config: Config, version: str = __version__) -> ReimportResult | None:
    """Run ``reimport`` once when ``version`` differs from the one stored in the database.

    Any difference counts, not only a newer version: after a downgrade the older parser re-parses, and the
    next upgrade re-parses again. Returns the result, or None when nothing had to run. Raises on failure
    without storing the version, so the next start tries again; callers use :func:`run_on_start`, which
    never raises.
    """
    if not config.auto_reimport:
        return None
    with file_lock(config.data_dir / "upgrade.lock"):  # sync and serve may start at the same time
        stored = stored_version(conn)
        if stored == version:
            return None
        if stored is None and not conn.execute("SELECT 1 FROM purchases LIMIT 1").fetchone():
            store_version(conn, version)  # a new database: nothing to re-parse
            return None
        log.info(
            "version changed from %s to %s: re-parsing the saved purchases once (GROCERIES_AUTO_REIMPORT)",
            stored or "a version that did not record it",
            version,
        )
        res = reimport(conn, config, stored_only=True)
        _log_result(res)
        if res.store_failed:  # e.g. "database is locked" by another writer: try again on the next start
            raise RuntimeError(f"{res.store_failed} re-parsed purchases could not be stored")
        if res.failed:
            # parse errors: the same files would fail on every start, so report them once. A fixed parser comes
            # with a newer version, which re-parses everything again; `groceries reimport` shows them meanwhile.
            log.warning("upgrade: %d saved purchases could not be re-parsed; see the warnings above", res.failed)
        store_version(conn, version)
        return res


def run_on_start(config: Config) -> None:
    """auto_reimport() on a connection of its own; a failure is logged and never stops sync or serve."""
    try:
        conn = connect(config.db_path)
    except Exception:
        log.warning("automatic reimport: cannot open the database (ignored)", exc_info=True)
        return
    try:
        auto_reimport(conn, config)
    except Exception:
        log.warning(
            "automatic reimport after the upgrade failed (ignored, retried on the next start); "
            "run `groceries reimport --dry-run` to see why",
            exc_info=True,
        )
    finally:
        try:
            conn.rollback()  # never leave a write transaction open: it would lock the database for the sync
        finally:
            conn.close()


def start_in_background(config: Config) -> threading.Thread:
    """auto_reimport() for `serve`: the dashboard answers right away, also while a sync holds the locks."""
    t = threading.Thread(target=run_on_start, args=(config,), name="auto-reimport", daemon=True)
    t.start()
    return t


def _log_result(res: ReimportResult) -> None:
    for d in res.changed:
        before = "new" if d.new else f"{d.total_before:.2f}"
        log.info(
            "upgrade: %s: total %s -> %.2f, %d lines added, %d removed%s",
            d.id,
            before,
            d.total_after,
            len(d.lines_added),
            len(d.lines_removed),
            f"; {', '.join(d.other)}" if d.other else "",
        )
    for retailer, name, title in res.titles_dropped:
        log.info("upgrade: %s product %r: dropped search match %r", retailer, name, title)
    s = res.summary()
    log.info(
        "upgrade: %d purchases checked, %d changed, %d raw files without a stored purchase skipped, "
        "%d search matches dropped, %d unused products removed",
        s["raw_files"],
        s["receipts_changed"],
        s["raw_files_skipped"],
        s["product_titles_dropped"],
        s["products_pruned"],
    )
