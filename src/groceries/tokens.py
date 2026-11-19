"""Persistent OAuth token pairs whose refresh token rotates on every use.

Losing the newest refresh token means logging in again, so every token response is
written to disk (atomically, mode 600) before anything uses it, under a lock shared by
all processes on the host.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .providers.base import AuthError
from .storage import ensure_private_dir, file_lock, write_json_atomic

log = logging.getLogger(__name__)


def extract_code(pasted: str) -> str:
    """Accept the bare authorization code or the whole redirect URL (scheme://...?code=...)."""
    pasted = pasted.strip().strip('"').strip("'")
    if "code=" in pasted:
        qs = parse_qs(urlparse(pasted).query or pasted.split("?", 1)[-1])
        if qs.get("code"):
            return qs["code"][0]
    return pasted


def extract_param(pasted: str, name: str) -> str | None:
    qs = parse_qs(urlparse(pasted.strip().strip('"').strip("'")).query)
    return qs[name][0] if qs.get(name) else None


def mask_email(email: str | None) -> str | None:
    if not email or "@" not in email:
        return None
    local, _, domain = email.partition("@")
    return f"{local[:1]}{'*' * max(2, len(local) - 1)}@{domain}"


def jwt_claims(token: str | None) -> dict[str, Any]:
    import base64

    try:
        payload = token.split(".")[1]  # type: ignore[union-attr]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (AttributeError, IndexError, ValueError):
        return {}


class RotatingTokenStore:
    """Subclasses implement ``_refresh`` (and usually an exchange method of their own)."""

    slug = "?"
    label = "?"

    def __init__(self, secrets_dir: Path, account: int | str | None = None) -> None:
        """`account`: the account id (one token file per store account); None is the single-account layout."""
        self.dir = secrets_dir
        stem = self.slug if account is None else f"{self.slug}-{account}"
        self.path = secrets_dir / f"{stem}-token.json"
        self.lock = secrets_dir / f"{stem}-token.lock"

    def load(self) -> dict[str, Any]:
        try:
            return json.loads(self.path.read_text())
        except FileNotFoundError:
            raise AuthError(f"no {self.label} token at {self.path}; run `groceries login {self.slug}`") from None

    def save(self, tok: dict[str, Any], previous: dict[str, Any] | None = None) -> dict[str, Any]:
        if not tok.get("refresh_token") and previous:
            tok = {**tok, "refresh_token": previous.get("refresh_token")}  # server chose not to rotate
        if not tok.get("access_token") or not tok.get("refresh_token"):
            raise AuthError(f"{self.label} token response lacks access_token/refresh_token")
        tok = {k: v for k, v in tok.items() if k != "id_token"}  # identity claims are not needed
        tok["obtained_at"] = int(time.time())
        ensure_private_dir(self.dir)
        write_json_atomic(self.path, tok)
        return tok

    def _refresh(self, tok: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def access_token(self, force_refresh: bool = False) -> str:
        with file_lock(self.lock):
            tok = self.load()  # re-read under the lock: another process may have rotated it
            expires = int(tok.get("obtained_at", 0)) + int(tok.get("expires_in", 0)) - 300
            if force_refresh or time.time() >= expires:
                tok = self.save(self._refresh(tok), previous=tok)
                log.info("refreshed %s access token", self.label)
            return tok["access_token"]
