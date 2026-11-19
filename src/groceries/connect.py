"""Connecting retailer accounts from the web UI.

A login is started server-side: we create an unguessable ``state``, keep it (plus any PKCE
verifier) in the secrets dir for 15 minutes, and hand the browser the retailer's authorize
URL together with an HttpOnly, SameSite=Strict cookie holding the same state. The code that
comes back (the Connect helper sends the login tab to /connect/callback, or it is pasted by hand) is only
accepted in a same-origin request carrying that cookie, and, where the retailer echoes it
(Jumbo), with the same OAuth state. Codes are exchanged server-side; tokens never leave the
secrets dir.
"""

from __future__ import annotations

import json
import re
import secrets
import time
from pathlib import Path
from typing import Any

from .config import Config
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


_APP_URL = re.compile(r"""(?:appie|jumbo)://[^\s'"<>]+""")
_FORM_CODE = re.compile(r"""name=["']code["'][^>]*value=["']([^"']+)""", re.I)
_FORM_STATE = re.compile(r"""name=["']state["'][^>]*value=["']([^"']+)""", re.I)


def parse_pasted(pasted: str) -> tuple[str, str | None]:
    """Code (and state, if present) from a redirect URL, a form_post HTML page, or a bare code."""
    from .tokens import extract_code, extract_param

    text = pasted.strip()
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
        self.labels_path = config.data_dir / "connections.json"

    # -- pending logins ---------------------------------------------------------------------------
    def _pending_path(self, slug: str) -> Path:
        return self.config.secrets_dir / f"{slug}-weblogin.json"

    def start(self, slug: str) -> dict[str, str]:
        if slug not in PROVIDERS:
            raise ConnectError(f"unknown retailer {slug}")
        state = secrets.token_urlsafe(24)
        url, private = get_provider(slug, self.config).begin_login(state)
        ensure_private_dir(self.config.secrets_dir)
        write_json_atomic(self._pending_path(slug), {"state": state, "created": int(time.time()), **private})
        return {"url": url, "state": state}

    def finish(
        self,
        slug: str,
        cookie_state: str | None,
        code: str,
        returned_state: str | None = None,
        verifier: str | None = None,
    ) -> str | None:
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
        try:
            label = get_provider(slug, self.config).complete_login(pending, code)
        except AuthError as e:
            raise ConnectError(str(e)) from None
        finally:
            path.unlink(missing_ok=True)  # codes are single-use either way
        self._set_label(slug, label)
        return label

    # -- status -----------------------------------------------------------------------------------
    def _labels(self) -> dict[str, Any]:
        try:
            return json.loads(self.labels_path.read_text())
        except (OSError, ValueError):
            return {}

    def _set_label(self, slug: str, label: str | None) -> None:
        labels = self._labels()
        labels[slug] = {"account": label, "connected_at": int(time.time())}
        write_json_atomic(self.labels_path, labels, 0o644)

    def disconnect(self, slug: str) -> None:
        if slug not in PROVIDERS:
            raise ConnectError(f"unknown retailer {slug}")
        lock = self.config.secrets_dir / f"{slug}-token.lock"
        with file_lock(lock):  # wait for a token refresh in progress, or it would write the file back
            for name in (f"{slug}-token.json", f"{slug}-weblogin.json", f"{slug}-login.json"):
                (self.config.secrets_dir / name).unlink(missing_ok=True)
        labels = self._labels()
        labels.pop(slug, None)
        write_json_atomic(self.labels_path, labels, 0o644)

    def is_connected(self, slug: str) -> bool:
        return (self.config.secrets_dir / f"{slug}-token.json").is_file()

    def status(self, conn: Any) -> list[dict[str, Any]]:
        labels = self._labels()
        out = []
        for slug in PROVIDERS:
            last = conn.execute(
                "SELECT started_at, finished_at, status, message FROM sync_runs WHERE retailer = ? ORDER BY id DESC "
                "LIMIT 1",
                (slug,),
            ).fetchone()
            count = conn.execute(
                "SELECT COUNT(*), MIN(local_date), MAX(local_date) FROM purchases WHERE retailer = ?", (slug,)
            ).fetchone()
            connected = self.is_connected(slug)
            needs = bool(last and last["status"] == "error" and (last["message"] or "").startswith("login needed"))
            out.append(
                {
                    "retailer": slug,
                    "name": DISPLAY.get(slug, slug),
                    "connected": connected,
                    "needs_reconnect": connected and needs,
                    "account": (labels.get(slug) or {}).get("account"),
                    "connected_at": (labels.get(slug) or {}).get("connected_at"),
                    "last_sync": dict(last) if last else None,
                    "purchases": count[0],
                    "first": count[1],
                    "last": count[2],
                }
            )
        return out
