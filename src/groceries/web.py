"""FastAPI app: a JSON API over Analytics plus the single-page dashboard."""

from __future__ import annotations

import base64
import html
import ipaddress
import re
import secrets
import sqlite3
from collections.abc import Iterator
from importlib import resources
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from . import __version__
from .accounts import migrate_legacy
from .analytics import Analytics
from .config import Config
from .db import connect
from .web_connect import register as register_connections

PEER_KEY = "groceries.peer"


def _networks(spec: str) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    return [ipaddress.ip_network(p.strip(), strict=False) for p in spec.split(",") if p.strip()]


def _in_networks(addr: str | None, networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network]) -> bool:
    try:
        ip = ipaddress.ip_address(addr or "")
    except ValueError:
        return False
    return any(ip in n for n in networks)


class _RecordPeer:
    """Outermost ASGI layer: remember the TCP peer before X-Forwarded-For rewrites scope["client"]."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] in ("http", "websocket"):
            scope[PEER_KEY] = (scope.get("client") or (None,))[0]
        await self.app(scope, receive, send)


_HOST_RE = re.compile(r"\[([0-9a-f:.]+)\](?::\d{1,5})?|([a-z0-9.-]+)(?::\d{1,5})?")

ECHARTS_SRI = "sha384-Mx5lkUEQPM1pOJCwFtUICyX45KNojXbkWdYhkKUKsbv391mavbfoAmONbzkgYPzR"


def create_app(config: Config) -> FastAPI:
    app = FastAPI(title="groceries", docs_url=None, redoc_url=None, openapi_url=None)
    conn = connect(config.db_path)  # create/migrate the schema (and single-account logins) once at startup
    migrate_legacy(conn, config)
    conn.close()
    page = resources.files("groceries").joinpath("static/index.html").read_text()
    page = page.replace("{{ECHARTS_URL}}", html.escape(config.echarts_url))
    # pin the default CDN file by hash; a self-hosted copy (GROCERIES_ECHARTS_URL) is trusted as is
    sri = f' integrity="{ECHARTS_SRI}"' if config.echarts_url == Config.echarts_url else ""
    page = page.replace("{{ECHARTS_SRI}}", sri).replace("{{VERSION}}", __version__)

    # DNS rebinding: a hostile site can point its own name at this server and then talk to it
    # "same-origin". Only answer to Host names we expect: localhost, IP literals, the bind host
    # and GROCERIES_ALLOWED_HOSTS.
    allowed = {"localhost", config.host.lower()} | {
        h.strip().lower() for h in config.allowed_hosts.split(",") if h.strip()
    }

    @app.middleware("http")
    async def host_check(request: Request, call_next: Any) -> Response:
        host = request.headers.get("host", "").lower()
        m = _HOST_RE.fullmatch(host)
        name = (m.group(1) or m.group(2)) if m else host
        try:
            ipaddress.ip_address(name)
            ok = m is not None
        except ValueError:
            ok = m is not None and name in allowed
        if not ok and request.url.path != "/healthz":
            return Response(f"unknown host {name!r}; add it to GROCERIES_ALLOWED_HOSTS", status_code=421)
        return await call_next(request)

    if config.basic_auth:
        expected = "Basic " + base64.b64encode(config.basic_auth.encode()).decode()

        @app.middleware("http")
        async def basic_auth(request: Request, call_next: Any) -> Response:
            given = request.headers.get("authorization", "").encode("latin-1", "replace")
            if request.url.path != "/healthz" and not secrets.compare_digest(given, expected.encode()):
                return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="groceries"'})
            return await call_next(request)

    if config.auth_proxy_header:
        if not config.trusted_proxies:
            raise ValueError("GROCERIES_AUTH_PROXY_HEADER needs GROCERIES_TRUSTED_PROXIES (the proxy's address)")
        header = config.auth_proxy_header.lower()
        proxies = _networks(config.trusted_proxies)

        @app.middleware("http")
        async def proxy_auth(request: Request, call_next: Any) -> Response:
            if request.url.path == "/healthz":
                return await call_next(request)
            # the header only counts when the connection itself comes from the proxy
            if not request.headers.get(header) or not _in_networks(request.scope.get(PEER_KEY), proxies):
                return Response("authentication required (reverse proxy)", status_code=403)
            return await call_next(request)

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Response:
        resp = await call_next(request)
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["X-Frame-Options"] = "DENY"
        return resp

    def db() -> Iterator[sqlite3.Connection]:
        # FastAPI may run the dependency and the endpoint on different threadpool threads
        conn = sqlite3.connect(f"file:{config.db_path}?mode=ro", uri=True, timeout=10, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def analytics(
        retailer: str | None = Query(None), account: int | None = Query(None), conn: sqlite3.Connection = Depends(db)
    ) -> Analytics:
        return Analytics(conn, retailer or None, account)

    register_connections(app, config, db)

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return page

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/api/overview")
    def overview(a: Analytics = Depends(analytics)) -> dict[str, Any]:
        return a.overview()

    @app.get("/api/retailers")
    def retailers(conn: sqlite3.Connection = Depends(db)) -> list[str]:
        return [r[0] for r in conn.execute("SELECT DISTINCT retailer FROM purchases ORDER BY 1")]

    @app.get("/api/months")
    def months(a: Analytics = Depends(analytics)) -> list[dict[str, Any]]:
        return a.months()

    @app.get("/api/categories")
    def categories(a: Analytics = Depends(analytics)) -> list[dict[str, Any]]:
        return a.categories()

    @app.get("/api/category-months")
    def category_months(a: Analytics = Depends(analytics), top: int = Query(7, ge=1, le=7)) -> dict[str, Any]:
        return a.category_months(top)

    @app.get("/api/products")
    def products(
        a: Analytics = Depends(analytics), order: str = "spend", limit: int = Query(25, ge=1, le=500)
    ) -> list[dict[str, Any]]:
        return a.products(order, limit)

    @app.get("/api/price-changes")
    def price_changes(
        a: Analytics = Depends(analytics),
        limit: int = Query(25, ge=1, le=500),
        min_points: int = Query(4, ge=2),
        min_days: int = Query(120, ge=0),
    ) -> list[dict[str, Any]]:
        return a.price_changes(min_points, min_days, limit)

    @app.get("/api/price-history")
    def price_history(
        retailer: str, key: str, account: int | None = Query(None), conn: sqlite3.Connection = Depends(db)
    ) -> dict[str, Any]:
        return Analytics(conn, account=account).price_history(retailer, key)

    @app.get("/api/market-changes")
    def market_changes(
        a: Analytics = Depends(analytics), limit: int = Query(25, ge=1, le=500), by: str = "spend"
    ) -> list[dict[str, Any]]:
        return a.market_changes(limit, by)

    @app.get("/api/price-index")
    def price_index(a: Analytics = Depends(analytics)) -> list[dict[str, Any]]:
        return a.inflation()

    @app.get("/api/stores")
    def stores(a: Analytics = Depends(analytics)) -> list[dict[str, Any]]:
        return a.stores()

    @app.get("/api/weekdays")
    def weekdays(a: Analytics = Depends(analytics)) -> list[dict[str, Any]]:
        return a.weekdays()

    @app.get("/api/discounts")
    def discounts(a: Analytics = Depends(analytics)) -> list[dict[str, Any]]:
        return a.discounts_by_type()

    @app.get("/api/purchases")
    def purchases(a: Analytics = Depends(analytics), limit: int = Query(50, ge=1, le=1000)) -> list[dict[str, Any]]:
        return a.purchases(limit)

    @app.get("/api/purchases/{purchase_id}")
    def purchase(purchase_id: str, a: Analytics = Depends(analytics)) -> dict[str, Any]:
        p = a.purchase(purchase_id)
        if p is None:
            raise HTTPException(404, "no such purchase")
        p.pop("raw_file", None)
        return p

    @app.exception_handler(sqlite3.OperationalError)
    async def no_db(_: Request, exc: sqlite3.OperationalError) -> JSONResponse:
        return JSONResponse({"detail": f"database unavailable: {exc}; run `groceries sync` first"}, status_code=503)

    # added last = outermost: record the TCP peer, then honour X-Forwarded-* from trusted proxies only
    if config.trusted_proxies:
        app.add_middleware(ProxyHeadersMiddleware, trusted_hosts=config.trusted_proxies)
    app.add_middleware(_RecordPeer)
    return app
