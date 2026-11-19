"""Connecting retailer accounts from the web UI.

A login is started server-side: we create an unguessable ``state``, keep it (plus any PKCE
verifier) in the secrets dir for 15 minutes, and hand the browser the retailer's authorize
URL together with an HttpOnly, SameSite=Strict cookie holding the same state. The code that
comes back (the Connect helper sends the login tab to /connect/callback, or it is pasted by hand) is only
accepted in a same-origin request carrying that cookie, and, where the retailer echoes it
(Jumbo), with the same OAuth state. Codes are exchanged server-side; tokens never leave the
secrets dir.

The pending login also records which account slot it is for (an existing account being
reconnected, or a new one), so a code can only ever land in that slot. Codes are exchanged into
a staging file first; a login that turns out to be a store account that is already connected
is refused and discarded.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any

from . import accounts
from .config import Config
from .db import connect
from .providers import PROVIDERS, get_provider
from .providers.base import AuthError
from .storage import ensure_private_dir, file_lock, write_json_atomic

PENDING_TTL = 15 * 60
COOKIE_PREFIX = "gc_login_"
DISPLAY = {"ah": "Albert Heijn", "jumbo": "Jumbo"}


def _same(given: object, expected: str) -> bool:
    """Constant-time compare that tolerates non-str or non-ASCII input from a request body."""
    return secrets.compare_digest(str(given).encode(), expected.encode())


class ConnectError(Exception):
    pass


MAX_PASTED = 32 * 1024
_APP_URL = re.compile(r"""(?:appie|jumbo)://[^\s'"<>]+""")
_FORM_CODE = re.compile(r"""name=["']code["'][^>]*value=["']([^"']+)""", re.I)
_FORM_STATE = re.compile(r"""name=["']state["'][^>]*value=["']([^"']+)""", re.I)


def parse_pasted(pasted: str) -> tuple[str, str | None]:
    """Code (and state, if present) from a redirect URL, a form_post HTML page, or a bare code."""
    from .tokens import extract_code, extract_param

    text = pasted.strip()
    if len(text) > MAX_PASTED:  # a console line or form_post page is a few KB; also bounds the regex work
        raise ConnectError("what was pasted is too long; paste only the red 'Failed to launch' line")
    m = _FORM_CODE.search(text)  # a form_post page (its action is a jumbo:// URL too, so check this first)
    if m:
        s = _FORM_STATE.search(text)
        return m.group(1), s.group(1) if s else None
    # a DevTools console line ("Failed to launch 'jumbo://…?code=…&state=…' because …") or text around the URL
    url = _APP_URL.search(text)
    if url:
        text = url.group(0)
    return extract_code(text), extract_param(text, "state") if "state=" in text else None


class Connections:
    def __init__(self, config: Config) -> None:
        self.config = config

    def _db(self) -> Any:
        conn = connect(self.config.db_path)
        accounts.migrate_legacy(conn, self.config)
        return conn

    # -- pending logins ---------------------------------------------------------------------------
    def _pending_path(self, slug: str) -> Path:
        return self.config.secrets_dir / f"{slug}-weblogin.json"

    def start(self, slug: str, account_id: int | None = None, label: str | None = None) -> dict[str, str]:
        """Begin a login for an existing account slot (reconnect) or for a new account (`account_id` None).
        The slot is kept server-side with the state, so the code that comes back can only land there."""
        if slug not in PROVIDERS:
            raise ConnectError(f"unknown retailer {slug}")
        conn = self._db()
        try:
            if account_id is not None:
                acc = accounts.get(conn, account_id)
                if not acc or acc["retailer"] != slug or acc["status"] == "removed":
                    raise ConnectError("no such account")
            elif label is not None:
                label = accounts.clean_label(label)
                accounts._check_free(conn, slug, label)
        except accounts.AccountError as e:
            raise ConnectError(str(e)) from None
        finally:
            conn.close()
        state = secrets.token_urlsafe(24)
        url, private = get_provider(slug, self.config).begin_login(state)
        ensure_private_dir(self.config.secrets_dir)
        write_json_atomic(
            self._pending_path(slug),
            {"state": state, "created": int(time.time()), "account_id": account_id, "label": label, **private},
        )
        return {"url": url, "state": state}

    def finish(
        self,
        slug: str,
        cookie_state: str | None,
        code: str,
        returned_state: str | None = None,
        verifier: str | None = None,
    ) -> dict[str, Any]:
        """`verifier`: the Connect helper may run its own PKCE authorization in the already logged-in
        retailer session and send its verifier along; the cookie check still binds it to this login."""
        path = self._pending_path(slug)
        try:
            pending = json.loads(path.read_text())
        except (OSError, ValueError):
            raise ConnectError("no login in progress; start again") from None
        if not cookie_state or not _same(cookie_state, pending["state"]):
            raise ConnectError("this login was started in another browser or has been replaced; start again")
        if returned_state and not _same(returned_state, pending["state"]):
            raise ConnectError("the code belongs to a different login attempt; start again")
        if time.time() - pending["created"] > PENDING_TTL:
            path.unlink(missing_ok=True)
            raise ConnectError("the login took longer than 15 minutes; start again")
        if not code or len(code) > 4096:
            raise ConnectError("no code found in what was pasted")
        if verifier:
            if not 32 <= len(verifier) <= 128:
                raise ConnectError("invalid PKCE verifier")
            pending = {**pending, "verifier": verifier}
        # exchange into a staging token file: a login that turns out to be a duplicate must not replace anything
        staging = f"staging-{secrets.token_hex(6)}"
        provider = get_provider(slug, self.config, account=staging)
        try:
            try:
                display = provider.complete_login(pending, code)
            except AuthError as e:
                raise ConnectError(str(e)) from None
            finally:
                path.unlink(missing_ok=True)  # codes are single-use either way
            try:
                identity = accounts.identity_hash(slug, provider.identity())
            except Exception:  # noqa: BLE001  (without it duplicates cannot be detected, but the login works)
                identity = None
            return self._attach(slug, pending.get("account_id"), pending.get("label"), staging, display, identity)
        finally:
            for name in (f"{slug}-{staging}-token.json", f"{slug}-{staging}-token.lock"):
                (self.config.secrets_dir / name).unlink(missing_ok=True)

    def _attach(
        self,
        slug: str,
        account_id: int | None,
        label: str | None,
        staging: str,
        display: str | None,
        identity: str | None,
    ) -> dict[str, Any]:
        name = DISPLAY.get(slug, slug)
        conn = self._db()
        try:
            dup = accounts.duplicate_of(conn, slug, identity, account_id or -1)
            if dup and dup["status"] != "removed":
                raise ConnectError(
                    f'this {name} login is already connected as "{dup["label"]}". To add another account, '
                    f"log out of {name} first or use a private window, then try again"
                )
            if account_id is not None:
                acc = accounts.get(conn, account_id)
                if not acc:
                    raise ConnectError("the account was removed while logging in; start again")
                if identity and acc["identity"] and acc["identity"] != identity:
                    raise ConnectError(
                        f'you logged in to a different {name} account than "{acc["label"]}". '
                        f'Log in as that account, or use "Add another {name} account"'
                    )
            else:
                if dup:  # a removed account logs in again: bring it back with its purchases
                    account_id = dup["id"]
                else:
                    try:
                        account_id = accounts.create(conn, slug, label)
                    except accounts.AccountError as e:
                        raise ConnectError(str(e)) from None
            staged = self.config.secrets_dir / f"{slug}-{staging}-token.json"
            with file_lock(accounts.token_lock(self.config, slug, account_id)):
                os.replace(staged, accounts.token_path(self.config, slug, account_id))
            with conn:
                conn.execute(
                    "UPDATE accounts SET display = COALESCE(?, display), identity = COALESCE(?, identity), "
                    "connected_at = ?, status = 'connected' WHERE id = ?",
                    (display, identity, int(time.time()), account_id),
                )
            acc = accounts.get(conn, account_id) or {}
            return {"account_id": account_id, "label": acc.get("label"), "account": acc.get("display")}
        finally:
            conn.close()

    # -- status -----------------------------------------------------------------------------------
    def is_connected(self, account_id: int) -> bool:
        conn = self._db()
        try:
            acc = accounts.get(conn, account_id)
        finally:
            conn.close()
        return bool(acc) and accounts.token_path(self.config, acc["retailer"], account_id).is_file()

    def status(self, conn: Any) -> list[dict[str, Any]]:
        out = []
        for slug in PROVIDERS:
            items = []
            for a in accounts.list_accounts(conn, self.config, slug):
                if a["status"] == "removed":
                    continue
                last = conn.execute(
                    "SELECT started_at, finished_at, status, message FROM sync_runs WHERE account_id = ? "
                    "ORDER BY id DESC LIMIT 1",
                    (a["id"],),
                ).fetchone()
                count = conn.execute(
                    "SELECT COUNT(*), MIN(local_date), MAX(local_date) FROM purchases WHERE account_id = ?",
                    (a["id"],),
                ).fetchone()
                needs = bool(last and last["status"] == "error" and (last["message"] or "").startswith("login needed"))
                items.append(
                    {
                        "id": a["id"],
                        "label": a["label"],
                        "account": a["display"],
                        "connected": a["connected"],
                        "needs_reconnect": a["connected"] and needs,
                        "connected_at": a["connected_at"],
                        "last_sync": dict(last) if last else None,
                        "purchases": count[0],
                        "first": count[1],
                        "last": count[2],
                    }
                )
            out.append({"retailer": slug, "name": DISPLAY.get(slug, slug), "accounts": items})
        return out
