"""Command line: `groceries login|import-token|sync|serve|stats`."""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import sys

from .config import Config
from .providers import PROVIDERS, get_provider


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="groceries", description="Sync and analyze supermarket purchase history.")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("login", help="log in to a retailer and store its tokens")
    p.add_argument("retailer", choices=sorted(PROVIDERS))

    p = sub.add_parser("import-token", help="bootstrap from a refresh token (read from stdin or prompt)")
    p.add_argument("retailer", choices=sorted(PROVIDERS))

    p = sub.add_parser("sync", help="pull new purchases")
    p.add_argument("retailers", nargs="*", help="default: GROCERIES_RETAILERS")
    p.add_argument("--full", action="store_true", help="re-fetch every purchase, not only new ones")
    p.add_argument("--refresh-products", action="store_true", help="look up every product again")

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
    sub.add_parser("stats", help="print a summary as JSON")

    args = ap.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    config = Config.from_env()

    if args.cmd == "login":
        get_provider(args.retailer, config).interactive_login()
    elif args.cmd == "import-token":
        provider = get_provider(args.retailer, config)
        token = sys.stdin.readline() if not sys.stdin.isatty() else getpass.getpass("Refresh token: ")
        provider.import_refresh_token(token)  # type: ignore[attr-defined]
        print("Token rotated and saved.")
    elif args.cmd == "sync":
        from .sync import run_sync

        failed = False
        targets = (
            args.retailers
            or config.retailers
            or sorted(s for s in PROVIDERS if (config.secrets_dir / f"{s}-token.json").is_file())
        )
        if not targets:
            logging.warning("nothing to sync: no retailer connected (use the web UI or `groceries login ah`)")
        for slug in targets:
            try:
                run_sync(config, slug, full=args.full, refresh_products=args.refresh_products)
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

        from .web import create_app

        uvicorn.run(
            create_app(config),
            host=config.host,
            port=config.port,
            log_level="info",
            proxy_headers=False,  # the app applies X-Forwarded-* itself, for GROCERIES_TRUSTED_PROXIES only
        )
    elif args.cmd == "stats":
        from .analytics import Analytics
        from .db import connect

        print(json.dumps(Analytics(connect(config.db_path)).tldr(), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
