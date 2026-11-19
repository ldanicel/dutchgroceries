"""Retailer providers. Register new ones in PROVIDERS."""

from __future__ import annotations

from ..config import Config
from .base import Provider


def _ah(config: Config) -> Provider:
    from .ah import AlbertHeijn

    return AlbertHeijn(config)


def _jumbo(config: Config) -> Provider:
    from .jumbo import Jumbo

    return Jumbo(config)


PROVIDERS = {"ah": _ah, "jumbo": _jumbo}


def get_provider(slug: str, config: Config) -> Provider:
    try:
        factory = PROVIDERS[slug]
    except KeyError:
        raise ValueError(f"unknown retailer {slug!r}; known: {', '.join(PROVIDERS)}") from None
    return factory(config)
