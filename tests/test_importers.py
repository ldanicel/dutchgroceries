from __future__ import annotations

import json
import subprocess
from datetime import date
from pathlib import Path

import pytest

from groceries import sync
from groceries.analytics import Analytics
from groceries.config import Config
from groceries.db import connect
from groceries.importers import bank, checkjebon

from .conftest import FakeProvider

ING_CSV = """\
"Datum";"Naam / Omschrijving";"Rekening";"Tegenrekening";"Code";"Af Bij";"Bedrag (EUR)";"Mutatiesoort";"Mededelingen"
"20230105";"Albert Heijn 1234 VOORBEELDSTAD";"NL00TEST0000000000";"";"BA";"Af";"23,45";"Betaalautomaat";"Pasvolgnr: 001"
"20230105";"Albert Heijn 1234 VOORBEELDSTAD";"NL00TEST0000000000";"";"BA";"Af";"23,45";"Betaalautomaat";"Pasvolgnr: 001"
"20230110";"JUMBO VOORBEELDSTAD";"NL00TEST0000000000";"";"BA";"Af";"1.012,00";"Betaalautomaat";""
"20230111";"Albert Heijn 1234 VOORBEELDSTAD";"NL00TEST0000000000";"";"BA";"Bij";"2,50";"Betaalautomaat";"Retour"
"20230112";"Bakker Test";"NL00TEST0000000000";"";"BA";"Af";"4,10";"Betaalautomaat";""
"20250401";"AH to go Station";"NL00TEST0000000000";"";"BA";"Af";"3,00";"Betaalautomaat";""
"""


def test_ing_rows() -> None:
    rows = bank.parse_rows(ING_CSV, bank.PRESETS["ing"])
    assert [(r["retailer"], r["date"], r["amount"]) for r in rows] == [
        ("ah", "2023-01-05", 23.45),
        ("ah", "2023-01-05", 23.45),
        ("jumbo", "2023-01-10", 1012.0),
        ("ah", "2023-01-11", -2.5),
        ("ah", "2025-04-01", 3.0),
    ]
    assert rows[0]["id"] != rows[1]["id"]  # same-day duplicates are distinct payments
    assert all("NL00TEST" not in r["description"] for r in rows)  # account numbers are not stored


def test_generic_signed_amounts() -> None:
    csv_text = "date,amount,description\n2023-02-01,-12.50,ALBERT HEIJN 9999\n2023-02-02,-5.00,Other shop\n"
    rows = bank.parse_rows(csv_text, bank.PRESETS["generic"])
    assert [(r["date"], r["amount"]) for r in rows] == [("2023-02-01", 12.5)]


def test_bank_import_is_idempotent_and_fills_months_before_receipts(
    config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sync, "get_provider", lambda slug, cfg, account=None: FakeProvider(cfg))
    sync.run_sync(config, "ah")  # receipts start 2025-03-01
    f = tmp_path / "ing.csv"
    f.write_text(ING_CSV)
    conn = connect(config.db_path)
    assert bank.import_csv(conn, f)["added"] == 5
    assert bank.import_csv(conn, f)["added"] == 0
    months = {m["month"]: m for m in Analytics(conn).months()}
    assert months["2023-01"]["bank"] == round(23.45 * 2 + 1012.0 - 2.5, 2)
    assert months["2025-04"]["bank"] == 0  # after AH receipts begin, receipts count, not the bank
    assert months["2025-03"]["spend"] > 0


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _snapshot(price: float) -> str:
    return json.dumps(
        [
            {
                "n": "ah",
                "d": [
                    {"n": "Test melk", "l": "wi500001/test-melk", "p": price, "s": "1 l"},
                    {"n": "Other", "l": "wi999999/other", "p": 1.0, "s": "1 st"},
                ],
            },
            {"n": "jumbo", "d": [{"n": "Test melk", "l": "jumbo-test", "p": 9.99}]},
        ]
    )


def test_parse_snapshot() -> None:
    assert checkjebon.parse_snapshot(_snapshot(1.29)) == {"500001": (1.29, "1 l"), "999999": (1.0, "1 st")}


def test_checkjebon_import(config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sync, "get_provider", lambda slug, cfg, account=None: FakeProvider(cfg))
    sync.run_sync(config, "ah")  # the order fixture maps product 100001 to webshop id 500001
    origin = tmp_path / "origin"
    (origin / "data").mkdir(parents=True)
    _git(origin, "init", "-q", "-b", "main")
    for day, price in [("2022-11-06", 1.00), ("2022-11-07", 1.05), ("2023-06-01", 1.10), ("2026-11-01", 1.30)]:
        (origin / "data" / "supermarkets.json").write_text(_snapshot(price))
        _git(origin, "add", "-A")
        env = f"{day}T12:00:00"
        subprocess.run(
            [
                "git",
                "-C",
                str(origin),
                "-c",
                "user.name=t",
                "-c",
                "user.email=t@example.invalid",
                "commit",
                "-q",
                "-m",
                day,
                "--date",
                env,
            ],
            check=True,
            capture_output=True,
            env={"GIT_COMMITTER_DATE": env, "PATH": "/usr/bin:/bin"},
        )
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", "--no-checkout", str(origin), str(clone)], check=True, capture_output=True)
    conn = connect(config.db_path)
    res = checkjebon.import_prices(conn, clone, since=date(2022, 11, 1), every_days=7, fetch=False)
    assert res["snapshots"] == 3  # 11-07 is within 7 days of 11-06
    assert (res["new_products"], res["matched_new"]) == (2, 1)  # 100002 has no public price
    again = checkjebon.import_prices(conn, clone, since=date(2022, 11, 1), every_days=7, fetch=False)
    assert (again["new_products"], again["rows"]) == (0, 0)  # nothing newer, misses are not rescanned
    a = Analytics(conn)
    [chg] = a.market_changes(5)
    assert (chg["first_month"], chg["old_price"], chg["last_month"], chg["new_price"]) == (
        "2022-11",
        1.0,
        "2026-11",
        1.3,
    )
    hist = a.price_history("ah", "100001")
    assert [r["source"] for r in hist["regular"]].count("public") == 3 and hist["unit"] == "pack"


def test_refresh_skips_within_a_week(config: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    monkeypatch.setattr(checkjebon, "import_prices", lambda conn, repo: calls.append(1) or {"rows": 0})
    conn = connect(config.db_path)
    assert checkjebon.refresh_if_due(conn, config.data_dir) == {"rows": 0}
    assert checkjebon.refresh_if_due(conn, config.data_dir) is None
    assert calls == [1]


def test_price_failure_does_not_fail_sync_or_alert(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from groceries import cli

    monkeypatch.setenv("GROCERIES_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("GROCERIES_PUBLIC_PRICES", "true")
    monkeypatch.setattr(sync, "get_provider", lambda slug, cfg, account=None: FakeProvider(cfg))
    sent: list[str] = []
    monkeypatch.setattr("groceries.notify.Notifier.send", lambda self, title, *a, **k: sent.append(title) or True)

    def boom(*a: object, **k: object) -> None:
        raise RuntimeError("git unavailable")

    monkeypatch.setattr(checkjebon, "import_prices", boom)
    assert cli.main(["sync", "ah"]) == 0
    assert sent == []


def test_demo_seed_is_synthetic_and_refuses_real_db(config: Config) -> None:
    from groceries.demo import seed

    counts = seed(config.data_dir, months=6)
    assert counts["purchases"] > 20
    a = Analytics(connect(config.db_path))
    assert a.overview()["purchases"] == counts["purchases"]
    assert all(p["name"].startswith("Demo ") for p in a.products("spend", 50))
    conn = connect(config.db_path)
    labels = {tuple(r) for r in conn.execute("SELECT retailer, label FROM accounts")}
    assert labels == {("ah", "Main"), ("ah", "Sam"), ("jumbo", "Main")}
    ah_owners = conn.execute("SELECT COUNT(DISTINCT account_id) FROM purchases WHERE retailer = 'ah'").fetchone()[0]
    assert ah_owners == 2
    assert conn.execute("SELECT COUNT(*) FROM purchases WHERE account_id IS NULL").fetchone()[0] == 0
    with pytest.raises(SystemExit):
        seed(config.data_dir)
