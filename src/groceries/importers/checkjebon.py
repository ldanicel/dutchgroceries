"""Backfill shelf prices from the git history of the open checkjebon dataset (MIT).

https://github.com/supermarkt/checkjebon commits ``data/supermarkets.json`` almost daily
since November 2022: every product of AH, Jumbo and others with its listed price. Each
commit is a snapshot, so sampling commits gives a price history per product.

Only products you have bought are stored. AH entries are matched on the webshop id in
their link (``wi<id>/...``), which the AH provider records per product. A listed price
can be a promotion price on that day, so trends should use medians over several samples.

Requires the ``git`` command. The clone is a blob-less partial clone (about 150 MB after a full backfill); snapshot
files are fetched one at a time on demand.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import subprocess
from datetime import UTC, date, timedelta
from pathlib import Path

log = logging.getLogger(__name__)

REPO_URL = "https://github.com/supermarkt/checkjebon.git"
DATA_PATH = "data/supermarkets.json"
SOURCE = "checkjebon"
_AH_ID = re.compile(r"^wi(\d+)")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


def ensure_repo(repo: Path, url: str = REPO_URL) -> None:
    if (repo / ".git").is_dir() or (repo / "HEAD").is_file():
        _git(repo, "fetch", "--quiet", "origin")
        return
    repo.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "clone", "--quiet", "--filter=blob:none", "--no-checkout", url, str(repo)], check=True)


def snapshot_commits(repo: Path, since: date, every_days: int) -> list[tuple[str, str]]:
    """One (date, commit) per `every_days`, oldest first, from the commits touching the data file."""
    out = _git(
        repo, "log", "origin/HEAD", "--format=%ad %H", "--date=short", f"--since={since.isoformat()}", "--", DATA_PATH
    )
    by_day: dict[str, str] = {}
    for line in reversed(out.splitlines()):
        day, commit = line.split()
        by_day[day] = commit  # last commit of the day wins
    picked: list[tuple[str, str]] = []
    next_day = since
    for day in sorted(by_day):
        d = date.fromisoformat(day)
        if d >= next_day:
            picked.append((day, by_day[day]))
            next_day = d + timedelta(days=every_days)
    return picked


def parse_snapshot(raw: bytes | str, retailer: str = "ah") -> dict[str, tuple[float, str | None]]:
    """webshop id -> (price, unit size) for one retailer in one snapshot."""
    data = json.loads(raw)
    out: dict[str, tuple[float, str | None]] = {}
    for shop in data:
        if shop.get("n") != retailer:
            continue
        for item in shop.get("d") or []:
            m = _AH_ID.match(item.get("l") or "")
            price = item.get("p")
            if m and isinstance(price, int | float) and price > 0:
                out[m.group(1)] = (float(price), item.get("s"))
    return out


def _store(
    conn: sqlite3.Connection, repo: Path, commits: list[tuple[str, str]], wanted: dict[str, str], retailer: str
) -> tuple[int, set[str]]:
    rows, seen = 0, set()
    for day, commit in commits:
        raw = subprocess.run(
            ["git", "-C", str(repo), "cat-file", "-p", f"{commit}:{DATA_PATH}"], check=True, capture_output=True
        ).stdout
        prices = parse_snapshot(raw, retailer)
        batch = [
            (retailer, key, day, prices[ws][0], prices[ws][1], SOURCE) for ws, key in wanted.items() if ws in prices
        ]
        seen.update(b[1] for b in batch)
        with conn:
            conn.executemany(
                """INSERT INTO market_prices (retailer, product_key, date, price, unit_size, source)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT DO UPDATE SET price = excluded.price, unit_size = excluded.unit_size""",
                batch,
            )
        rows += len(batch)
    return rows, seen


def import_prices(
    conn: sqlite3.Connection,
    repo: Path,
    since: date = date(2022, 11, 1),
    every_days: int = 7,
    retailer: str = "ah",
    fetch: bool = True,
    full: bool = False,
) -> dict[str, int]:
    """Incremental by default: products scanned before only get snapshots after the newest stored
    date; products bought since the last run get their whole history once."""
    if fetch:
        ensure_repo(repo)
    wanted = {
        r["webshop_id"]: r["product_key"]
        for r in conn.execute(
            """SELECT DISTINCT p.webshop_id, p.product_key FROM products p
               JOIN purchase_lines l ON l.retailer = p.retailer AND l.product_key = p.product_key
               WHERE p.retailer = ? AND p.webshop_id IS NOT NULL""",
            (retailer,),
        )
    }
    scanned = (
        set()
        if full
        else {
            r[0]
            for r in conn.execute(
                "SELECT product_key FROM market_scans WHERE retailer = ? AND source = ?", (retailer, SOURCE)
            )
        }
    )
    last = (
        None
        if full
        else conn.execute(
            "SELECT MAX(date) FROM market_prices WHERE retailer = ? AND source = ?", (retailer, SOURCE)
        ).fetchone()[0]
    )
    new = {ws: k for ws, k in wanted.items() if k not in scanned}
    old = {ws: k for ws, k in wanted.items() if k in scanned}
    all_commits = snapshot_commits(repo, since, every_days)
    later = [c for c in all_commits if last and c[0] > last] if old else []
    log.info(
        "checkjebon: %d new products over %d snapshots, %d known products over %d newer snapshots",
        len(new),
        len(all_commits) if new else 0,
        len(old),
        len(later),
    )
    rows, seen = _store(conn, repo, all_commits, new, retailer) if new else (0, set())
    rows2, seen2 = _store(conn, repo, later, old, retailer) if later else (0, set())
    with conn:
        conn.executemany(
            "INSERT OR IGNORE INTO market_scans (retailer, product_key, source) VALUES (?,?,?)",
            [(retailer, k, SOURCE) for k in new.values()],
        )
    return {
        "snapshots": len(all_commits) if new else len(later),
        "products": len(wanted),
        "new_products": len(new),
        "matched_new": len(seen),
        "rows": rows + rows2,
    }


def refresh_if_due(conn: sqlite3.Connection, data_dir: Path, every_days: int = 7) -> dict[str, int] | None:
    """Incremental import at most once per `every_days`; None when skipped. Callers treat errors as non-fatal."""
    from datetime import datetime

    from ..storage import write_json_atomic

    state_path = data_dir / "public-prices.json"
    try:
        last = datetime.fromisoformat(json.loads(state_path.read_text())["last_run"])
        if datetime.now(UTC) - last < timedelta(days=every_days):
            return None
    except (OSError, ValueError, KeyError):
        pass
    res = import_prices(conn, data_dir / "checkjebon")
    write_json_atomic(state_path, {"last_run": datetime.now(UTC).isoformat(timespec="seconds"), **res}, 0o644)
    return res
