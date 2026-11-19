"""Optional failure/recovery notifications (ntfy and/or a generic JSON webhook).

One alert when a store account's sync starts failing, one when it works again; nothing in
between, so a daily timer does not repeat the same alert every night.
"""

from __future__ import annotations

import base64
import json
import logging
from collections import defaultdict

import httpx

from .config import Config
from .storage import write_json_atomic

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.state_path = config.data_dir / "alert-state.json"

    @property
    def enabled(self) -> bool:
        return bool(self.config.ntfy_url or self.config.webhook_url)

    def send(self, title: str, message: str, priority: str = "default", tags: str = "") -> bool:
        ok = True
        if self.config.ntfy_url:
            # ntfy decodes RFC 2047 titles, which keeps emoji intact through an HTTP header
            headers = {
                "Title": "=?UTF-8?B?" + base64.b64encode(title.encode()).decode() + "?=",
                "Priority": priority,
                "Tags": tags,
                "User-Agent": "groceries/1.0",
            }
            if self.config.ntfy_token:
                headers["Authorization"] = f"Bearer {self.config.ntfy_token}"
            ok &= self._post(self.config.ntfy_url, content=message.encode(), headers=headers)
        if self.config.webhook_url:
            payload = {
                "title": title,
                "message": message,
                "priority": priority,
                "tags": tags.split(",") if tags else [],
            }
            ok &= self._post(
                self.config.webhook_url,
                content=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json", "User-Agent": "groceries/1.0"},
            )
        if not self.enabled:
            log.info("notifications not configured; would send: %s - %s", title, message)
        return ok

    @staticmethod
    def _post(url: str, content: bytes, headers: dict[str, str]) -> bool:
        for attempt in range(3):
            try:
                httpx.post(url, content=content, headers=headers, timeout=15).raise_for_status()
                return True
            except httpx.HTTPError as e:
                log.warning("notification to %s failed (attempt %d): %s", httpx.URL(url).host, attempt + 1, e)
        return False

    # -- state ------------------------------------------------------------------------------
    def _load(self) -> dict[str, dict[str, str]]:
        try:
            return json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            return {}

    def failed(
        self, key: str, retailer: str, error: str, account: str | None = None, legacy: str | None = None
    ) -> None:
        """`key` identifies the account in the state file; `account` is its label for the title."""
        state = self._load()
        if key in state or (legacy and legacy in state):
            return
        reason = error.split(":", 1)[0] if error.startswith(("login needed", "RuntimeError", "HTTP")) else error
        title = _title(self.config.alert_fail_title, retailer=retailer, reason=reason[:60], account=account)
        if self.send(title, error[:1000], self.config.ntfy_priority, "warning") or not self.enabled:
            state[key] = {"error": error[:500]}
            write_json_atomic(self.state_path, state, 0o644)

    def succeeded(
        self, key: str, retailer: str, message: str, account: str | None = None, legacy: str | None = None
    ) -> None:
        """`legacy`: the key a single-account version used (the retailer name), resolved by the first account."""
        state = self._load()
        if key not in state and not (legacy and legacy in state):
            return
        title = _title(self.config.alert_resolved_title, retailer=retailer, reason="", account=account)
        if self.send(title, message, "default", "white_check_mark") or not self.enabled:
            state.pop(key, None)
            if legacy:
                state.pop(legacy, None)
            write_json_atomic(self.state_path, state, 0o644)


def _title(template: str, account: str | None = None, **fields: str) -> str:
    """Fill {retailer}/{reason}/{account}; a typo in a user-supplied template must not hide the real error.

    {account} becomes " (Label)". A template without it gets the label appended, so alerts always say
    which store account failed.
    """
    suffix = f" ({account})" if account else ""
    try:
        title = template.format_map(defaultdict(str, {**fields, "account": suffix}))
    except (ValueError, IndexError):
        return f"{fields.get('retailer', '')}{suffix}: {template}"
    return title if "{account}" in template or not suffix else title + suffix
