"""Store accounts: several AH or Jumbo logins per installation, combined into one household archive.

An account is a row in the ``accounts`` table plus a token file ``<retailer>-<id>-token.json`` in
the secrets dir. Purchases record the account that synced them; a receipt that shows up in two
accounts (a shared card) is stored once, under the account that saw it first.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
from pathlib import Path
from typing import Any

from .config import Config
from .db import main_account
from .providers import PROVIDERS
from .storage import file_lock

log = logging.getLogger(__name__)

MAX_LABEL = 40


class AccountError(ValueError):
    pass


def token_path(config: Config, retailer: str, account_id: int) -> Path:
    return config.secrets_dir / f"{retailer}-{account_id}-token.json"


def token_lock(config: Config, retailer: str, account_id: int) -> Path:
    return config.secrets_dir / f"{retailer}-{account_id}-token.lock"


def identity_hash(retailer: str, value: str | None) -> str | None:
    """What identifies a store account without storing the e-mail address or member id itself."""
    if not value:
        return None
    return hashlib.sha256(f"{retailer}:{value.strip().lower()}".encode()).hexdigest()


def clean_label(label: Any) -> str:
    text = " ".join(str(label or "").split())
    if not text:
        raise AccountError("the name must not be empty")
    if len(text) > MAX_LABEL:
        raise AccountError(f"the name is longer than {MAX_LABEL} characters")
    return text


def migrate_legacy(conn: sqlite3.Connection, config: Config) -> None:
    """Move a single-account install (``<retailer>-token.json``, connections.json) onto account ids.

    Idempotent; db.connect() already gave existing purchases a "Main" account per retailer.
    """
    labels_path = config.data_dir / "connections.json"
    try:
        labels = json.loads(labels_path.read_text())
    except (OSError, ValueError):
        labels = {}
    for retailer in PROVIDERS:
        legacy = config.secrets_dir / f"{retailer}-token.json"
        if not legacy.is_file() and retailer not in labels:
            continue
        with conn:
            row = conn.execute("SELECT id FROM accounts WHERE retailer = ? ORDER BY id LIMIT 1", (retailer,)).fetchone()
            account_id = row[0] if row else main_account(conn, retailer)
            info = labels.get(retailer) or {}
            conn.execute(
                "UPDATE accounts SET display = COALESCE(display, ?), connected_at = COALESCE(connected_at, ?) "
                "WHERE id = ?",
                (info.get("account"), info.get("connected_at"), account_id),
            )
        if legacy.is_file():
            with file_lock(config.secrets_dir / f"{retailer}-token.lock"):  # a refresh may be rotating it
                target = token_path(config, retailer, account_id)
                if legacy.is_file() and not target.exists():
                    os.replace(legacy, target)
                    log.info("moved the %s login to account %d", retailer, account_id)
    if labels:
        os.replace(labels_path, labels_path.with_suffix(".json.migrated"))


def get(conn: sqlite3.Connection, account_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
    return dict(row) if row else None


def list_accounts(conn: sqlite3.Connection, config: Config, retailer: str | None = None) -> list[dict[str, Any]]:
    sql, args = "SELECT * FROM accounts", []
    if retailer:
        sql, args = sql + " WHERE retailer = ?", [retailer]
    out = []
    for row in conn.execute(sql + " ORDER BY retailer, id", args):
        a = dict(row)
        a["connected"] = token_path(config, a["retailer"], a["id"]).is_file()
        out.append(a)
    return out


def find(conn: sqlite3.Connection, ref: str, retailer: str | None = None) -> dict[str, Any]:
    """An account by id or by label (case-insensitive), optionally within one retailer."""
    rows = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM accounts WHERE (CAST(id AS TEXT) = ? OR lower(label) = lower(?))"
            + (" AND retailer = ?" if retailer else ""),
            [ref, ref, *([retailer] if retailer else [])],
        )
    ]
    if not rows:
        raise AccountError(f"no account {ref!r}" + (f" for {retailer}" if retailer else ""))
    if len(rows) > 1:
        raise AccountError(f"{ref!r} matches several accounts; use the id or the retailer")
    return rows[0]


def create(conn: sqlite3.Connection, retailer: str, label: str | None = None) -> int:
    if retailer not in PROVIDERS:
        raise AccountError(f"unknown retailer {retailer}")
    with conn:
        if label is None:
            n = conn.execute("SELECT COUNT(*) FROM accounts WHERE retailer = ?", (retailer,)).fetchone()[0]
            label = "Main" if n == 0 else f"Account {n + 1}"
            while conn.execute(
                "SELECT 1 FROM accounts WHERE retailer = ? AND lower(label) = lower(?)", (retailer, label)
            ).fetchone():
                n += 1
                label = f"Account {n + 1}"
        label = clean_label(label)
        _check_free(conn, retailer, label)
        return int(conn.execute("INSERT INTO accounts (retailer, label) VALUES (?, ?)", (retailer, label)).lastrowid)


def _check_free(conn: sqlite3.Connection, retailer: str, label: str, except_id: int | None = None) -> None:
    taken = conn.execute(
        "SELECT id FROM accounts WHERE retailer = ? AND lower(label) = lower(?) AND id IS NOT ?",
        (retailer, label, except_id),
    ).fetchone()
    if taken:
        raise AccountError(f"there already is a {retailer} account called {label!r}")


def rename(conn: sqlite3.Connection, account_id: int, label: str) -> str:
    acc = get(conn, account_id)
    if not acc:
        raise AccountError("no such account")
    label = clean_label(label)
    _check_free(conn, acc["retailer"], label, account_id)
    with conn:
        conn.execute("UPDATE accounts SET label = ? WHERE id = ?", (label, account_id))
    return label


def disconnect(conn: sqlite3.Connection, config: Config, account_id: int) -> None:
    """Delete the stored login; the account row and its purchases stay."""
    acc = get(conn, account_id)
    if not acc:
        raise AccountError("no such account")
    with file_lock(token_lock(config, acc["retailer"], account_id)):  # wait for a refresh in progress
        token_path(config, acc["retailer"], account_id).unlink(missing_ok=True)
        (config.secrets_dir / f"{acc['retailer']}-{account_id}-login.json").unlink(missing_ok=True)
    with conn:
        conn.execute("UPDATE accounts SET status = 'disconnected' WHERE id = ? AND status != 'removed'", (account_id,))


def remove(conn: sqlite3.Connection, config: Config, account_id: int, purge: bool = False) -> int:
    """Forget an account's login and hide it from Connections. Its purchases stay, still labelled,
    unless `purge`: then they and the account are deleted. Returns how many purchases were deleted."""
    disconnect(conn, config, account_id)
    with conn:
        if not purge:
            conn.execute("UPDATE accounts SET status = 'removed' WHERE id = ?", (account_id,))
            return 0
        deleted = conn.execute("DELETE FROM purchases WHERE account_id = ?", (account_id,)).rowcount
        conn.execute("DELETE FROM sync_runs WHERE account_id = ?", (account_id,))
        conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
    return deleted


def duplicate_of(
    conn: sqlite3.Connection, retailer: str, identity: str | None, account_id: int
) -> dict[str, Any] | None:
    if not identity:
        return None
    row = conn.execute(
        "SELECT * FROM accounts WHERE retailer = ? AND identity = ? AND id != ?", (retailer, identity, account_id)
    ).fetchone()
    return dict(row) if row else None
