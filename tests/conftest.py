from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from groceries.config import Config
from groceries.providers.ah import STAMPS_PRODUCT, order_products, parse_order, parse_receipt
from groceries.providers.base import ProductInfo, Provider, Purchase, PurchaseRef

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
def config(tmp_path: Path) -> Config:
    # "testserver" is the Host header FastAPI's TestClient sends
    return Config(
        data_dir=tmp_path / "data", secrets_dir=tmp_path / "secrets", request_delay=0, allowed_hosts="testserver"
    )


class FakeProvider(Provider):
    """Serves the synthetic fixtures through the real AH parsers, without HTTP."""

    slug = "ah"
    display_name = "AH"

    def __init__(self, config: Config) -> None:
        super().__init__(config)
        self.receipt = load("ah_receipt.json")
        self.order = load("ah_order.json")
        self.looked_up: list[str] = []

    def check_auth(self) -> None:
        pass

    def list_purchases(self) -> Iterator[PurchaseRef]:
        yield PurchaseRef("store", self.receipt["summary"]["id"], summary=self.receipt["summary"])
        yield PurchaseRef("online", str(self.order["summary"]["orderId"]), summary=self.order["summary"])

    def fetch_purchase(self, ref: PurchaseRef) -> tuple[dict[str, Any], Purchase]:
        raw = self.receipt if ref.channel == "store" else self.order
        return raw, self.parse_raw(ref.channel, raw)

    def parse_raw(self, channel: str, raw: dict[str, Any]) -> Purchase:
        if channel == "store":
            return parse_receipt(raw["summary"], raw["detail"])
        return parse_order(raw["summary"], raw["detail"])

    def known_products(self, raw: dict[str, Any]) -> Iterator[ProductInfo]:
        if "groupedProductsInTaxonomy" in raw["detail"]:
            yield from order_products(raw["detail"])
        else:
            yield STAMPS_PRODUCT

    def lookup_products(self, items: list[tuple[str, str]]) -> Iterator[ProductInfo]:
        for key, _name in items:
            self.looked_up.append(key)
            yield ProductInfo(
                product_key=key, lookup_state="ok", title=f"Product {key}", category="Groente, aardappelen"
            )
