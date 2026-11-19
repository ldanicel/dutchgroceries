"""The interface every retailer provider implements, plus the normalized records it returns."""

from __future__ import annotations

import abc
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any

from ..config import Config


class AuthError(RuntimeError):
    """Credentials are missing, expired or rejected; a human has to log in again."""


@dataclass
class Line:
    name: str
    amount: float
    quantity: float = 1
    product_key: str | None = None
    unit_price: float | None = None
    weight: float | None = None
    weight_unit: str | None = None
    deposit: float | None = None
    is_bonus: bool = False


@dataclass
class Discount:
    amount: float  # positive: money saved
    name: str | None = None
    type: str | None = None


@dataclass
class Payment:
    method: str | None
    amount: float


@dataclass
class Purchase:
    retailer: str
    channel: str  # 'store' | 'online'
    external_id: str
    ts_utc: str
    total: float
    lines: list[Line] = field(default_factory=list)
    discounts: list[Discount] = field(default_factory=list)
    payments: list[Payment] = field(default_factory=list)
    store_id: str | None = None
    store_street: str | None = None
    store_city: str | None = None
    store_postcode: str | None = None
    status: str | None = None

    @property
    def id(self) -> str:
        return f"{self.retailer}:{self.channel}:{self.external_id}"

    @property
    def discount_total(self) -> float:
        return round(sum(d.amount for d in self.discounts), 2)


@dataclass
class PurchaseRef:
    """One entry of a retailer's purchase list; enough to decide whether to fetch details."""

    channel: str
    external_id: str
    final: bool = True  # False for e.g. an online order that is not delivered yet
    summary: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProductInfo:
    product_key: str
    lookup_state: str  # 'ok', 'unmapped' (no catalogue id) or 'missing' (catalogue has no such product)
    webshop_id: str | None = None
    title: str | None = None
    brand: str | None = None
    category: str | None = None
    subcategory: str | None = None
    unit_size: str | None = None
    category_inferred: bool = False  # True when guessed from a search match, not the catalogue
    raw: dict[str, Any] | None = None


class Provider(abc.ABC):
    slug: str
    display_name: str

    def __init__(self, config: Config, account: int | str | None = None) -> None:
        """`account`: which store account's tokens to use (see accounts.py); None is the single-account layout."""
        self.config = config
        self.account = account

    @abc.abstractmethod
    def check_auth(self) -> None:
        """Raise AuthError if the stored credentials cannot be used."""

    @abc.abstractmethod
    def list_purchases(self) -> Iterator[PurchaseRef]:
        """Yield every purchase the retailer still has on record, newest first."""

    @abc.abstractmethod
    def fetch_purchase(self, ref: PurchaseRef) -> tuple[dict[str, Any], Purchase]:
        """Return (raw JSON to archive, normalized purchase)."""

    def known_products(self, raw: dict[str, Any]) -> Iterable[ProductInfo]:
        """Product details that came for free with a purchase (e.g. online order lines)."""
        return ()

    @abc.abstractmethod
    def lookup_products(self, items: list[tuple[str, str]]) -> Iterator[ProductInfo]:
        """Look up catalogue details (title, brand, category) for (product key, receipt name) pairs."""

    def begin_login(self, state: str) -> tuple[str, dict[str, Any]]:
        """Web login: (authorize URL, private data to keep until the code comes back)."""
        raise NotImplementedError(f"{self.display_name} has no web login")

    def complete_login(self, pending: dict[str, Any], code: str) -> str | None:
        """Exchange the code, store tokens; return a masked account label for display."""
        raise NotImplementedError(f"{self.display_name} has no web login")

    def identity(self) -> str | None:
        """A stable id of the logged-in store account (member id or e-mail), to refuse connecting it twice."""
        return None

    def interactive_login(self) -> None:
        raise NotImplementedError(f"{self.display_name} has no login helper")
