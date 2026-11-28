from __future__ import annotations

import sqlite3

import pytest

from groceries.categorize import LOCAL, Categorizer, infer_categories, rule_category
from groceries.db import SCHEMA


@pytest.mark.parametrize(
    ("name", "category"),
    [
        ("EXAMPLE CAMEMB & CROU", "Kaas"),
        ("PASTASAUS 5 KAZEN", "Soepen, sauzen, kruiden, olie"),  # the sauce, not the cheese
        ("SCHARREL EI 20ST M/L", "Zuivel, eieren"),  # a two-letter word still counts
        ("NIGIRI ZALM", "Vis"),
        ("KC ROSBIEF GEGR", "Vleeswaren"),
        ("KALKOENSCHNITZEL 2", "Vlees"),
        ("MINI TROSROMATOMATEN", "Groente, aardappelen"),  # compound word
        ("Example honingmeloen", "Fruit, verse sappen"),  # melon, not honey
        ("Biscuits framboos", "Koek, snoep, chocolade"),  # flavour, not fruit
        ("LISTERINE MW INTENSE", "Drogisterij"),
        ("WASMIDDEL WM GOUD", "Huishouden"),
        ("OPLAADBAAR AAA", "Koken, tafelen, vrije tijd"),
        ("Najaarsbloemen", "Koken, tafelen, vrije tijd"),
        ("BLOEMKOOL", "Groente, aardappelen"),
        ("XQZ 12", None),
    ],
)
def test_rules(name: str, category: str | None) -> None:
    assert rule_category(name) == category


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    rows = [
        # (retailer, key, title, name, category, inferred, state)
        ("ah", "1", "Example Geitenkaas Naturel", "GEITENKAAS", "Kaas", 0, "ok"),
        ("ah", "2", "Example Volkoren Spelt Crackers", "CRACKERS", "Ontbijtgranen, beleg", 0, "ok"),
        ("ah", "3", "Example Pils Krat", "PILS", "Bier en wijn", 0, "ok"),
        ("jumbo", "name:SPELT CRACKERS", "Other Spelt Crackers Volkoren", "SPELT CRACKERS", None, 0, "ok"),
        ("jumbo", "name:RODE WIJN", None, "RODE WIJN", None, 0, "missing"),
        ("jumbo", "name:XQZ", None, "XQZ", None, 0, "missing"),
        ("jumbo", "name:OLD", None, "GEITENKAAS", "Vis", LOCAL, "missing"),  # an older wrong guess
        ("ah", "9", "Example Product", "PRODUCT", "Kaas", 0, "ok"),  # from the retailer: never touched
    ]
    conn.executemany(
        "INSERT INTO products (retailer, product_key, title, name, category, category_inferred, lookup_state) "
        "VALUES (?,?,?,?,?,?,?)",
        rows,
    )
    return conn


def test_infer_categories() -> None:
    conn = _db()
    assert Categorizer(conn).similar("Other Spelt Crackers") == "Ontbijtgranen, beleg"
    counts = infer_categories(conn)
    got = dict(conn.execute("SELECT product_key, category FROM products"))
    assert got["name:SPELT CRACKERS"] == "Ontbijtgranen, beleg"  # similar product
    assert got["name:RODE WIJN"] == "Bier en wijn"  # rule, mapped to the spelling in the database
    assert got["name:XQZ"] is None
    assert got["name:OLD"] == "Kaas"  # inferred categories are worked out again
    assert got["9"] == "Kaas"
    assert counts == {"checked": 4, "rule": 2, "similar": 1, "unknown": 1, "changed": 3}
    flags = dict(conn.execute("SELECT product_key, category_inferred FROM products"))
    assert flags["name:SPELT CRACKERS"] == LOCAL and flags["name:XQZ"] == 0 and flags["1"] == 0
    assert infer_categories(conn)["changed"] == 0  # stable


def test_dry_run_writes_nothing() -> None:
    conn = _db()
    assert infer_categories(conn, dry_run=True)["changed"] == 3
    assert conn.execute("SELECT COUNT(*) FROM products WHERE category IS NULL").fetchone()[0] == 3
