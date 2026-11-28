"""Command line: `groceries login|import-token|sync|reimport|serve|stats|...`."""

from __future__ import annotations

import argparse
import contextlib
import getpass
import json
import logging
import sys

from . import __version__
from .config import Config
from .providers import PROVIDERS, get_provider


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="groceries", description="Sync and analyze supermarket purchase history.")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--version", action="version", version=f"dutchgroceries {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    acc_help = "store account name or id (default: the first account; a new name adds an account)"
    p = sub.add_parser("login", help="log in to a retailer and store its tokens")
    p.add_argument("retailer", choices=sorted(PROVIDERS))
    p.add_argument("--account", help=acc_help)

    p = sub.add_parser("import-token", help="bootstrap from a refresh token (read from stdin or prompt)")
    p.add_argument("retailer", choices=sorted(PROVIDERS))
    p.add_argument("--account", help=acc_help)

    p = sub.add_parser("accounts", help="list, rename or remove store accounts")
    asub = p.add_subparsers(dest="action", required=True)
    asub.add_parser("list", help="every store account and its state")
    q = asub.add_parser("rename", help="give an account another name")
    q.add_argument("account", help="name or id")
    q.add_argument("name")
    q = asub.add_parser("remove", help="delete an account's login (its purchases stay unless --purge)")
    q.add_argument("account", help="name or id")
    q.add_argument("--purge", action="store_true", help="also delete the purchases it synced")
    q.add_argument("--yes", action="store_true", help="do not ask for confirmation")

    p = sub.add_parser("sync", help="pull new purchases")
    p.add_argument("retailers", nargs="*", help="default: GROCERIES_RETAILERS")
    p.add_argument("--account", help="only this store account (name or id)")
    p.add_argument("--full", action="store_true", help="re-fetch every purchase, not only new ones")
    p.add_argument("--refresh-products", action="store_true", help="look up every product again")

    p = sub.add_parser("reimport", help="re-parse the saved raw responses and update the database (no network)")
    p.add_argument("--provider", choices=sorted(PROVIDERS), help="only this retailer (default: all)")
    p.add_argument("--account", help="only purchases of this store account (name or id)")
    p.add_argument("--receipt", help="only this purchase (receipt/order id or raw file name)")
    p.add_argument("--dry-run", action="store_true", help="show what would change, write nothing")

    p = sub.add_parser("export-raw", help="print the saved raw response of one purchase, e.g. for a bug report")
    p.add_argument("--receipt", required=True, help="receipt/order id or raw file name")
    p.add_argument("--redact", action="store_true", help="strip ids, card numbers, addresses and store details")
    p.add_argument("-o", "--output", help="write to this file instead of stdout")

    p = sub.add_parser("import-prices", help="backfill shelf prices from a public dataset (opt-in)")
    p.add_argument("source", choices=["checkjebon"])
    p.add_argument("--repo", help="where to keep the dataset clone (default: <data dir>/checkjebon)")
    p.add_argument("--since", default="2022-11-01", help="first snapshot date (YYYY-MM-DD)")
    p.add_argument("--every-days", type=int, default=7, help="sample one snapshot every N days")
    p.add_argument("--full", action="store_true", help="rescan every product over the whole history")

    p = sub.add_parser("import-bank", help="import supermarket card payments from a bank CSV export")
    p.add_argument("csv", nargs="+", help="CSV file(s) exported from your bank")
    p.add_argument("--format", default="ing", help="preset (ing, generic) or a mapping JSON file")

    p = sub.add_parser("demo-seed", help="fill an EMPTY data dir with synthetic demo purchases")
    p.add_argument("--months", type=int, default=24)

    sub.add_parser("serve", help="run the web dashboard")
    st = sub.add_parser("stats", help="print a summary as JSON")
    st.add_argument(
        "--range",
        choices=["all", "week", "month", "quarter", "year", "2y"],
        default="all",
        help="only the last week, month, quarter, year or 2 years (rolling, ending today)",
    )

    args = ap.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    config = Config.from_env()

    if args.cmd in ("login", "import-token"):
        import secrets as _secrets

        from . import accounts
        from .connect import ConnectError, Connections
        from .db import connect

        conn = connect(config.db_path)
        accounts.migrate_legacy(conn, config)
        account_id, label = None, args.account
        if args.account:
            with contextlib.suppress(accounts.AccountError):  # an unknown name adds an account
                account_id, label = accounts.find(conn, args.account, args.retailer)["id"], None
        else:
            first = [a for a in accounts.list_accounts(conn, config, args.retailer) if a["status"] != "removed"]
            account_id = first[0]["id"] if first else None
        staging = f"staging-{_secrets.token_hex(6)}"
        provider = get_provider(args.retailer, config, account=staging)
        try:
            if args.cmd == "login":
                provider.interactive_login()
            else:
                token = sys.stdin.readline() if not sys.stdin.isatty() else getpass.getpass("Refresh token: ")
                provider.import_refresh_token(token)  # type: ignore[attr-defined]
            try:
                identity = accounts.identity_hash(args.retailer, provider.identity())
            except Exception:  # noqa: BLE001
                identity = None
            res = Connections(config)._attach(args.retailer, account_id, label, staging, None, identity)
        except ConnectError as e:
            raise SystemExit(f"not saved: {e}") from None
        finally:
            for name in (f"{args.retailer}-{staging}-{x}" for x in ("token.json", "token.lock", "login.json")):
                (config.secrets_dir / name).unlink(missing_ok=True)
        print(f"Saved the login as {args.retailer} account {res['label']!r} (id {res['account_id']}).")
    elif args.cmd == "accounts":
        return _accounts(config, args)
    elif args.cmd == "sync":
        from . import accounts
        from .db import connect
        from .sync import run_sync
        from .upgrade import run_on_start

        conn = connect(config.db_path)
        accounts.migrate_legacy(conn, config)
        failed = False
        only = None
        if args.account:
            try:
                acc = accounts.find(conn, args.account, args.retailers[0] if len(args.retailers) == 1 else None)
            except accounts.AccountError as e:
                raise SystemExit(str(e)) from None
            only, args.retailers = acc["id"], [acc["retailer"]]
        run_on_start(config)
        targets = (
            args.retailers
            or config.retailers
            or sorted({a["retailer"] for a in accounts.list_accounts(conn, config) if a["connected"]})
        )
        if not targets:
            logging.warning("nothing to sync: no retailer connected (use the web UI or `groceries login ah`)")
        for slug in targets:
            try:
                run_sync(config, slug, full=args.full, refresh_products=args.refresh_products, account=only)
            except Exception:
                logging.exception("sync of %s failed", slug)
                failed = True
        if config.public_prices:
            from .db import connect
            from .importers import checkjebon

            try:  # optional extra: never fails the sync or raises an alert
                res = checkjebon.refresh_if_due(connect(config.db_path), config.data_dir)
                logging.info("public prices: %s", res if res is not None else "skipped, updated in the last 7 days")
            except Exception:
                logging.warning("public price refresh failed (ignored)", exc_info=True)
        return 1 if failed else 0
    elif args.cmd == "reimport":
        return _reimport(config, args)
    elif args.cmd == "export-raw":
        return _export_raw(config, args)
    elif args.cmd == "import-prices":
        from datetime import date
        from pathlib import Path

        from .db import connect
        from .importers import checkjebon

        repo = Path(args.repo) if args.repo else config.data_dir / "checkjebon"
        res = checkjebon.import_prices(
            connect(config.db_path), repo, date.fromisoformat(args.since), args.every_days, full=args.full
        )
        print(json.dumps(res))
    elif args.cmd == "import-bank":
        from pathlib import Path

        from .db import connect
        from .importers import bank

        conn = connect(config.db_path)
        for f in args.csv:
            print(f, json.dumps(bank.import_csv(conn, Path(f), args.format)))
    elif args.cmd == "demo-seed":
        from .demo import seed

        print(json.dumps(seed(config.data_dir, args.months)))
    elif args.cmd == "serve":
        import uvicorn

        from .upgrade import start_in_background
        from .web import create_app

        app = create_app(config)  # creates/migrates the schema
        start_in_background(config)
        uvicorn.run(
            app,
            host=config.host,
            port=config.port,
            log_level="info",
            proxy_headers=False,  # the app applies X-Forwarded-* itself, for GROCERIES_TRUSTED_PROXIES only
        )
    elif args.cmd == "stats":
        from .analytics import Analytics, range_start
        from .db import connect

        a = Analytics(connect(config.db_path), since=range_start(args.range))
        print(json.dumps(a.tldr(), indent=2, ensure_ascii=False))
    return 0


def _accounts(config: Config, args: argparse.Namespace) -> int:
    from . import accounts
    from .db import connect

    conn = connect(config.db_path)
    accounts.migrate_legacy(conn, config)
    try:
        if args.action == "list":
            for a in accounts.list_accounts(conn, config):
                n = conn.execute("SELECT COUNT(*) FROM purchases WHERE account_id = ?", (a["id"],)).fetchone()[0]
                state = "removed" if a["status"] == "removed" else "connected" if a["connected"] else "not connected"
                print(
                    f"{a['id']:>3}  {a['retailer']:<6} {a['label']:<20} {state:<14} {n:>5} purchases  "
                    f"last sync {a['last_sync'] or 'never'}  {a['display'] or ''}".rstrip()
                )
        elif args.action == "rename":
            acc = accounts.find(conn, args.account)
            print(f"renamed to {accounts.rename(conn, acc['id'], args.name)!r}")
        elif args.action == "remove":
            acc = accounts.find(conn, args.account)
            n = conn.execute("SELECT COUNT(*) FROM purchases WHERE account_id = ?", (acc["id"],)).fetchone()[0]
            what = f"and DELETE its {n} purchases" if args.purge else f"(its {n} purchases stay)"
            if not args.yes:
                answer = input(f"Remove {acc['retailer']} account {acc['label']!r} {what}? Type yes: ")
                if answer.strip().lower() != "yes":
                    print("nothing changed")
                    return 1
            deleted = accounts.remove(conn, config, acc["id"], purge=args.purge)
            print(f"removed {acc['label']!r}" + (f"; deleted {deleted} purchases" if args.purge else ""))
    except accounts.AccountError as e:
        raise SystemExit(str(e)) from None
    return 0


def _reimport(config: Config, args: argparse.Namespace) -> int:
    from . import accounts
    from .db import connect
    from .reimport import reimport

    conn = connect(config.db_path)
    accounts.migrate_legacy(conn, config)
    try:
        res = reimport(
            conn,
            config,
            [args.provider] if args.provider else None,
            account=args.account,
            receipt=args.receipt,
            dry_run=args.dry_run,
        )
    except accounts.AccountError as e:
        raise SystemExit(str(e)) from None
    for d in res.changed:
        before = "new" if d.new else f"{d.total_before:.2f}"
        print(f"{d.id}: total {before} -> {d.total_after:.2f}" + (f"; {', '.join(d.other)}" if d.other else ""))
        for line in d.lines_removed:
            print(f"  - {line}")
        for line in d.lines_added:
            print(f"  + {line}")
    for retailer, name, title in res.titles_dropped:
        print(f"{retailer} product {name!r}: drop search match {title!r}")
    summary = res.summary()
    if args.dry_run:
        summary["dry_run"] = True
    print(json.dumps(summary, indent=2))
    return 1 if res.failed else 0


def _export_raw(config: Config, args: argparse.Namespace) -> int:
    from pathlib import Path

    from .reimport import find_raw, redact

    try:
        retailer, channel, path = find_raw(config, args.receipt)
    except LookupError as e:
        raise SystemExit(str(e)) from None
    raw = json.loads(path.read_text())
    if args.redact:
        raw = redact(raw)
    else:
        logging.warning("not redacted: this file contains your receipt ids, store and payment details")
    text = json.dumps(raw, ensure_ascii=False, indent=1)
    # same format as the archive: dropped into <data dir>/raw/<retailer>/<channel>/, `reimport` reads it
    logging.info("%s %s purchase; it re-imports from raw/%s/%s/", retailer, channel, retailer, channel)
    if args.output:
        Path(args.output).write_text(text + "\n")
        logging.info("wrote %s", args.output)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
