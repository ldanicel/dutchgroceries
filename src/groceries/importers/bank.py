"""Import supermarket card payments from a bank statement CSV export.

Gives monthly spend for the years before a retailer's receipt history starts (amounts
only, no products). Nothing is fetched from a bank: you export the CSV yourself.

Formats are described by a small mapping (built-in presets, or a JSON file of your own):

    {
      "delimiter": ";",                      # optional, sniffed when omitted
      "encoding": "utf-8-sig",
      "date": {"column": "Datum", "format": "%Y%m%d"},
      "amount": {"column": "Bedrag (EUR)", "decimal": ","},
      "direction": {"column": "Af Bij", "debit": "Af"},   # omit when amounts are signed
      "text": ["Naam / Omschrijving", "Mededelingen"]
    }

Rows whose text matches a retailer pattern are kept; account numbers are never stored.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

PRESETS: dict[str, dict[str, Any]] = {
    # ING NL "Af- en bijschrijvingen" CSV, both the ';' and the older ',' variant
    "ing": {
        "encoding": "utf-8-sig",
        "date": {"column": "Datum", "format": "%Y%m%d"},
        "amount": {"column": "Bedrag (EUR)", "decimal": ","},
        "direction": {"column": "Af Bij", "debit": "Af"},
        "text": ["Naam / Omschrijving", "Mededelingen"],
    },
    # columns named date/amount/description, amounts signed (negative = paid), ISO dates
    "generic": {
        "encoding": "utf-8-sig",
        "date": {"column": "date", "format": "%Y-%m-%d"},
        "amount": {"column": "amount", "decimal": ".", "negative_is_debit": True},
        "text": ["description"],
    },
}

# Card payments show the store name in the counterparty/description. Online orders paid by
# iDEAL show up as "Albert Heijn" / "Jumbo" too. Gall & Gall, Etos etc. are not included.
RETAILER_PATTERNS: dict[str, str] = {
    "ah": r"\bALBERT\s*HEIJN\b|\bAH\s*(TO\s*GO|\d{3,5}|XL)\b|^AH\s",
    "jumbo": r"\bJUMBO\b(?!\s*(VLIEG|AIR))",
}


def load_mapping(fmt: str) -> dict[str, Any]:
    if fmt in PRESETS:
        return PRESETS[fmt]
    path = Path(fmt)
    if path.is_file():
        return json.loads(path.read_text())
    raise SystemExit(f"unknown bank format {fmt!r}: use one of {', '.join(PRESETS)} or a mapping JSON file")


def _amount(text: str, decimal: str) -> float:
    text = text.strip().replace("€", "").replace(" ", "")
    # "1.234,56" (comma decimal) or "1,234.56" (point decimal)
    text = text.replace(".", "").replace(",", ".") if decimal == "," else text.replace(",", "")
    return float(text)


def parse_rows(content: str, mapping: dict[str, Any], patterns: dict[str, str] | None = None) -> list[dict[str, Any]]:
    patterns = patterns or RETAILER_PATTERNS
    compiled = {k: re.compile(v, re.I) for k, v in patterns.items()}
    lines = content.splitlines()
    if not lines:
        raise ValueError("the CSV file is empty")
    delimiter = mapping.get("delimiter") or csv.Sniffer().sniff(lines[0], delimiters=";,\t").delimiter
    reader = csv.DictReader(io.StringIO(content), delimiter=delimiter)
    dcol, acol = mapping["date"], mapping["amount"]
    direction_col = (mapping.get("direction") or {}).get("column")
    needed = [dcol["column"], acol["column"], *(mapping.get("text") or []), *([direction_col] if direction_col else [])]
    missing = [c for c in needed if c not in (reader.fieldnames or [])]
    if missing:
        raise ValueError(f"CSV is missing column(s) {missing}; found {reader.fieldnames}. Wrong --format?")
    direction = mapping.get("direction")
    out: list[dict[str, Any]] = []
    occurrences: Counter[str] = Counter()
    for row in reader:
        text = " ".join((row.get(c) or "").strip() for c in mapping.get("text") or [])
        retailer = next((k for k, rx in compiled.items() if rx.search(text)), None)
        if not retailer:
            continue
        day = datetime.strptime(row[dcol["column"]].strip(), dcol["format"]).date().isoformat()
        value = _amount(row[acol["column"]], acol.get("decimal", "."))
        if direction:
            paid = value if row[direction["column"]].strip() == direction["debit"] else -value
        else:
            paid = -value if acol.get("negative_is_debit", True) else value
        key = f"{day}|{paid:.2f}|{text}"
        occurrences[key] += 1  # two identical payments on one day are both real
        row_id = hashlib.sha256(f"{key}|{occurrences[key]}".encode()).hexdigest()[:32]
        out.append(
            {
                "id": row_id,
                "retailer": retailer,
                "date": day,
                "amount": round(paid, 2),
                "description": re.sub(r"\s+", " ", text)[:200],
            }
        )
    return out


def import_csv(conn: sqlite3.Connection, path: Path, fmt: str = "ing") -> dict[str, int]:
    mapping = load_mapping(fmt)
    content = path.read_text(encoding=mapping.get("encoding", "utf-8-sig"))
    rows = parse_rows(content, mapping)
    with conn:
        before = conn.total_changes
        conn.executemany(
            """INSERT OR IGNORE INTO bank_payments (id, retailer, date, amount, description)
               VALUES (:id, :retailer, :date, :amount, :description)""",
            rows,
        )
        added = conn.total_changes - before
    return {"matched": len(rows), "added": added, **Counter(r["retailer"] for r in rows)}
