"""Runtime configuration, read from the environment (optionally via an env file)."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

_TRUE = ("1", "true", "yes", "y", "on", "enabled")
_FALSE = ("0", "false", "no", "n", "off", "disabled")


def load_env_file(path: str | os.PathLike[str] | None) -> None:
    """Load KEY=VALUE lines into os.environ without overriding variables already set."""
    if not path:
        return
    p = Path(path)
    if not p.is_file():
        return
    for raw in p.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


def env_flag(name: str, default: bool) -> bool:
    """A yes/no setting. Case, spaces and quotes do not matter; an empty value means the default, and so
    does a value that is neither yes nor no (with a warning)."""
    raw = os.environ.get(name)
    value = (raw or "").strip().strip("\"'").strip().lower()
    if not value:
        return default
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    log.warning("%s=%r is not a yes/no value; using the default (%s)", name, raw, str(default).lower())
    return default


@dataclass
class Config:
    data_dir: Path
    secrets_dir: Path
    host: str = "127.0.0.1"
    port: int = 8000
    timezone: str = "Europe/Amsterdam"
    request_delay: float = 0.5
    user_agent: str = "Appie/8.22.3"
    basic_auth: str = ""  # "user:password"; empty disables auth
    auth_proxy_header: str = ""  # e.g. "Remote-User": trust an authenticating reverse proxy, refuse requests without it
    allowed_hosts: str = ""  # extra Host names to serve (besides localhost, IP addresses and the bind host)
    trusted_proxies: str = ""  # IPs/CIDRs whose X-Forwarded-Proto/-For are honoured (TLS-terminating reverse proxy)
    echarts_url: str = "https://cdn.jsdelivr.net/npm/echarts@5.5.1/dist/echarts.min.js"
    ntfy_url: str = ""
    ntfy_token: str = ""
    ntfy_priority: str = "high"
    webhook_url: str = ""
    alert_fail_title: str = "⚠ {retailer} sync failed{account}"
    alert_resolved_title: str = "✅ Resolved: {retailer} sync{account}"
    retailers: list[str] = field(default_factory=list)  # empty: every retailer that has a stored login
    public_prices: bool = False  # refresh public shelf prices (checkjebon) after the sync, weekly
    auto_reimport: bool = True  # re-parse the saved purchases once after an upgrade (see upgrade.py)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "groceries.db"

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @classmethod
    def from_env(cls) -> Config:
        load_env_file(os.environ.get("GROCERIES_ENV_FILE"))
        env = os.environ.get
        data_dir = Path(env("GROCERIES_DATA_DIR", "./data")).resolve()
        return cls(
            data_dir=data_dir,
            secrets_dir=Path(env("GROCERIES_SECRETS_DIR", str(data_dir / "secrets"))).resolve(),
            host=env("GROCERIES_HOST", "127.0.0.1"),
            port=int(env("GROCERIES_PORT", "8000")),
            timezone=env("GROCERIES_TZ", "Europe/Amsterdam"),
            request_delay=float(env("GROCERIES_REQUEST_DELAY", "0.5")),
            user_agent=env("GROCERIES_AH_USER_AGENT", "Appie/8.22.3"),
            basic_auth=env("GROCERIES_BASIC_AUTH", ""),
            auth_proxy_header=env("GROCERIES_AUTH_PROXY_HEADER", ""),
            trusted_proxies=env("GROCERIES_TRUSTED_PROXIES", ""),
            allowed_hosts=env("GROCERIES_ALLOWED_HOSTS", ""),
            echarts_url=env("GROCERIES_ECHARTS_URL", cls.echarts_url),
            ntfy_url=env("GROCERIES_NTFY_URL", ""),
            ntfy_token=env("GROCERIES_NTFY_TOKEN", ""),
            ntfy_priority=env("GROCERIES_NTFY_PRIORITY", "high"),
            webhook_url=env("GROCERIES_WEBHOOK_URL", ""),
            alert_fail_title=env("GROCERIES_ALERT_FAIL_TITLE", cls.alert_fail_title),
            alert_resolved_title=env("GROCERIES_ALERT_RESOLVED_TITLE", cls.alert_resolved_title),
            retailers=[r.strip() for r in env("GROCERIES_RETAILERS", "").split(",") if r.strip()],
            public_prices=env_flag("GROCERIES_PUBLIC_PRICES", False),
            auto_reimport=env_flag("GROCERIES_AUTO_REIMPORT", True),
        )
