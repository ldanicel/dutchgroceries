"""Retailer providers. Register new ones in PROVIDERS."""

from __future__ import annotations

from ..config import Config
from .base import Provider


def _ah(config: Config, account: int | str | None = None) -> Provider:
    from .ah import AlbertHeijn

    return AlbertHeijn(config, account=account)


def _jumbo(config: Config, account: int | str | None = None) -> Provider:
    from .jumbo import Jumbo

    return Jumbo(config, account=account)


PROVIDERS = {"ah": _ah, "jumbo": _jumbo}


def get_provider(slug: str, config: Config, account: int | str | None = None) -> Provider:
    try:
        factory = PROVIDERS[slug]
    except KeyError:
        raise ValueError(f"unknown retailer {slug!r}; known: {', '.join(PROVIDERS)}") from None
    return factory(config, account=account)
