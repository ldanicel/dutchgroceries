"""SQLite schema shared by all retailers.

Every row carries a ``retailer`` slug (``ah``, ``jumbo``, ...). Purchases are in-store
receipts and online orders alike, told apart by ``channel``.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .storage import ensure_private_dir

SCHEMA = """
CREATE TABLE IF NOT EXISTS purchases (
    id             TEXT PRIMARY KEY,          -- '<retailer>:<channel>:<external id>'
    retailer       TEXT NOT NULL,
    channel        TEXT NOT NULL CHECK (channel IN ('store', 'online')),
    external_id    TEXT NOT NULL,
    ts_utc         TEXT NOT NULL,             -- ISO 8601, UTC
    local_date     TEXT NOT NULL,             -- YYYY-MM-DD in the configured timezone
    store_id       TEXT,
    store_street   TEXT,
    store_city     TEXT,
    store_postcode TEXT,
    total          REAL NOT NULL,             -- amount paid
    discount_total REAL NOT NULL DEFAULT 0,   -- positive number: what discounts saved
    item_count     INTEGER NOT NULL DEFAULT 0,
    status         TEXT,
    raw_file       TEXT,
    synced_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (retailer, channel, external_id)
);
CREATE INDEX IF NOT EXISTS purchases_date ON purchases (local_date);

CREATE TABLE IF NOT EXISTS purchase_lines (
    purchase_id  TEXT NOT NULL REFERENCES purchases (id) ON DELETE CASCADE,
    line_no      INTEGER NOT NULL,
    retailer     TEXT NOT NULL,
    product_key  TEXT,                        -- retailer-specific stable product id
    name         TEXT NOT NULL,
    quantity     REAL NOT NULL DEFAULT 1,
    weight       REAL,
    weight_unit  TEXT,
    unit_price   REAL,                        -- per piece, or per weight unit when weighed
    amount       REAL NOT NULL,               -- line total before receipt-level discounts
    deposit      REAL,
    is_bonus     INTEGER NOT NULL DEFAULT 0,
    qty_inferred INTEGER NOT NULL DEFAULT 0,  -- quantity reconstructed by normalize_quantities()
    PRIMARY KEY (purchase_id, line_no)
);
CREATE INDEX IF NOT EXISTS lines_product ON purchase_lines (retailer, product_key);

CREATE TABLE IF NOT EXISTS discounts (
    purchase_id TEXT NOT NULL REFERENCES purchases (id) ON DELETE CASCADE,
    idx         INTEGER NOT NULL,
    type        TEXT,
    name        TEXT,
    amount      REAL NOT NULL,                -- positive: money saved
    PRIMARY KEY (purchase_id, idx)
);

CREATE TABLE IF NOT EXISTS payments (
    purchase_id TEXT NOT NULL REFERENCES purchases (id) ON DELETE CASCADE,
    idx         INTEGER NOT NULL,
    method      TEXT,
    amount      REAL NOT NULL,
    PRIMARY KEY (purchase_id, idx)
);

CREATE TABLE IF NOT EXISTS products (
    retailer     TEXT NOT NULL,
    product_key  TEXT NOT NULL,
    webshop_id   TEXT,
    name         TEXT,                        -- short receipt name
    title        TEXT,                        -- full catalogue title
    brand        TEXT,
    category     TEXT,
    subcategory  TEXT,
    unit_size    TEXT,
    category_inferred INTEGER NOT NULL DEFAULT 0,
    lookup_state TEXT,                        -- NULL = not tried, 'ok', 'unmapped', 'missing'
    enriched_at  TEXT,
    PRIMARY KEY (retailer, product_key)
);

-- Shelf prices from public datasets (opt-in), for trends before your own receipts start.
CREATE TABLE IF NOT EXISTS market_prices (
    retailer    TEXT NOT NULL,
    product_key TEXT NOT NULL,                -- same key as purchase_lines.product_key
    date        TEXT NOT NULL,                -- YYYY-MM-DD of the snapshot
    price       REAL NOT NULL,                -- listed price; may be a promotion price that day
    unit_size   TEXT,
    source      TEXT NOT NULL,                -- e.g. 'checkjebon'
    PRIMARY KEY (retailer, product_key, date, source)
);

-- Which products a price source has already been searched for (so misses are not rescanned).
CREATE TABLE IF NOT EXISTS market_scans (
    retailer    TEXT NOT NULL,
    product_key TEXT NOT NULL,
    source      TEXT NOT NULL,
    PRIMARY KEY (retailer, product_key, source)
);

-- Card payments from bank statement exports: amounts only, for months without receipts.
CREATE TABLE IF NOT EXISTS bank_payments (
    id          TEXT PRIMARY KEY,             -- hash of the statement row (dedupes re-imports)
    retailer    TEXT NOT NULL,
    date        TEXT NOT NULL,                -- YYYY-MM-DD
    amount      REAL NOT NULL,                -- positive: paid; negative: refund
    description TEXT,
    source      TEXT NOT NULL DEFAULT 'bank',
    imported_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS bank_payments_date ON bank_payments (retailer, date);

CREATE TABLE IF NOT EXISTS sync_runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    retailer      TEXT NOT NULL,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    status        TEXT,
    new_purchases INTEGER,
    message       TEXT
);
"""


def connect(path: Path) -> sqlite3.Connection:
    # purchase history is personal data: the data directory is private to the service user
    ensure_private_dir(path.parent)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    if "qty_inferred" not in {r["name"] for r in conn.execute("PRAGMA table_info(purchase_lines)")}:
        conn.execute("ALTER TABLE purchase_lines ADD COLUMN qty_inferred INTEGER NOT NULL DEFAULT 0")
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(products)")}
    if "category_inferred" not in cols:
        conn.execute("ALTER TABLE products ADD COLUMN category_inferred INTEGER NOT NULL DEFAULT 0")
