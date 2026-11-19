"""Web routes for connecting retailer accounts (see connect.py for the security model)."""

from __future__ import annotations

import html
import json
import logging
import sqlite3
import threading
from collections.abc import Callable
from importlib import resources
from typing import Any
from urllib.parse import quote, urlsplit

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from .config import Config
from .connect import COOKIE_PREFIX, DISPLAY, PENDING_TTL, ConnectError, Connections, parse_pasted
from .providers import PROVIDERS

log = logging.getLogger(__name__)


def app_origin(request: Request) -> str:
    return f"{request.url.scheme}://{request.headers.get('host', request.url.netloc)}"


def same_origin(request: Request) -> bool:
    """POSTs must come from our own pages (blocks cross-site form/fetch CSRF)."""
    source = request.headers.get("origin") or request.headers.get("referer")
    if not source:
        return False
    parts = urlsplit(source)
    return f"{parts.scheme}://{parts.netloc}" == app_origin(request)


def bookmarklet(origin: str) -> str:
    js = resources.files("groceries").joinpath("static/helper.js").read_text()
    from .providers.jumbo import AUDIENCE, CLIENT_ID, REDIRECT_URI, SCOPE

    js = (
        js.replace("__APP__", json.dumps(origin)[1:-1])  # escaped for the JS string literal
        .replace("__JUMBO_CLIENT__", CLIENT_ID)
        .replace("__JUMBO_AUDIENCE__", AUDIENCE)
        .replace("__JUMBO_REDIRECT__", REDIRECT_URI)
        .replace("__JUMBO_SCOPE__", SCOPE)
    )
    body = " ".join(line.strip() for line in js.splitlines() if line.strip() and not line.strip().startswith("//"))
    return "javascript:" + quote(body, safe="(){}[];,=:!?'+*")  # '#' and '%' must not survive raw


def register(app: FastAPI, config: Config, db: Callable[..., Any]) -> None:
    connections = Connections(config)
    sync_lock = threading.Lock()

    def check_post(request: Request) -> None:
        if not same_origin(request):
            raise HTTPException(403, "cross-origin request refused")
        if not request.headers.get("content-type", "").startswith("application/json"):
            raise HTTPException(415, "JSON body required")

    def known(slug: str) -> str:
        if slug not in PROVIDERS:
            raise HTTPException(404, "unknown retailer")
        return slug

    @app.get("/api/connections")
    def status(request: Request, conn: sqlite3.Connection = Depends(db)) -> dict[str, Any]:
        return {
            "retailers": connections.status(conn),
            "syncing": sync_lock.locked(),
            "helper": bookmarklet(app_origin(request)),
        }

    @app.post("/api/connections/{slug}/start")
    def start(slug: str, request: Request) -> JSONResponse:
        check_post(request)
        try:
            res = connections.start(known(slug))
        except ConnectError as e:
            raise HTTPException(400, str(e)) from None
        resp = JSONResponse({"url": res["url"]})
        resp.set_cookie(
            COOKIE_PREFIX + slug,
            res["state"],
            max_age=PENDING_TTL,
            httponly=True,
            samesite="strict",
            secure=request.url.scheme == "https",
            path="/",
        )
        return resp

    @app.post("/api/connections/{slug}/finish")
    async def finish(slug: str, request: Request) -> JSONResponse:
        check_post(request)
        try:
            body = await request.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            raise HTTPException(400, "expected a JSON object")
        code, returned_state = parse_pasted(str(body.get("pasted") or body.get("code") or ""))
        returned_state = body.get("state") or returned_state
        cookie = request.cookies.get(COOKIE_PREFIX + slug)
        verifier = str(body.get("verifier") or "") or None
        try:
            # the code exchange is blocking HTTP: keep it off the event loop
            label = await run_in_threadpool(connections.finish, known(slug), cookie, code, returned_state, verifier)
        except ConnectError as e:
            raise HTTPException(400, str(e)) from None
        log.info("connected %s", slug)  # never log codes or tokens
        resp = JSONResponse({"connected": True, "account": label})
        resp.delete_cookie(COOKIE_PREFIX + slug, path="/")
        return resp

    @app.get("/connect/callback", response_class=HTMLResponse)
    def callback(provider: str) -> str:
        """Where the Connect helper sends the login tab. The code travels in the URL fragment (#code=…),
        which browsers never send to the server, so it stays out of access logs. Nothing happens until
        the user confirms; the page then posts the code same-origin, which only succeeds in the browser
        that started this login (pending-login cookie) within 15 minutes."""
        known(provider)
        name = html.escape(DISPLAY.get(provider, provider))
        slug = html.escape(provider)
        return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Connect {name}</title>
<style>body{{font:16px system-ui,sans-serif;max-width:32rem;margin:15vh auto;padding:0 16px;
background:#fcfcfb;color:#0b0b0b}} @media (prefers-color-scheme: dark){{body{{background:#111110;color:#f0efec}}}}
.ok{{color:#0ca30c}} .err{{color:#d03b3b}} a{{color:#2a78d6}}
button{{font:inherit;padding:8px 16px;border-radius:6px;border:0;background:#2a78d6;color:#fff;cursor:pointer}}</style>
</head><body><h1 id="h">Connect {name}?</h1>
<p id="msg">Your {name} login came back. Only continue if you just started connecting {name} in this
app yourself.</p><p><button id="go">Connect {name}</button></p>
<script>
const f = new URLSearchParams(location.hash.slice(1));
history.replaceState(null, "", location.pathname + "?provider={slug}");
const data = {{code: f.get("code") || "", state: f.get("state"), verifier: f.get("verifier")}};
const h = document.getElementById("h"), m = document.getElementById("msg"), go = document.getElementById("go");
if (!data.code) {{
  h.textContent = "Nothing to connect"; m.textContent = "This page did not receive a login code."; go.remove();
}}
go.onclick = async () => {{
  go.disabled = true;
  const r = await fetch("/api/connections/{slug}/finish", {{method: "POST",
    headers: {{"Content-Type": "application/json"}}, body: JSON.stringify(data)}});
  const j = await r.json().catch(() => ({{}}));
  go.remove();
  if (r.ok) {{
    h.textContent = "✓ {name} connected"; h.className = "ok";
    m.textContent = "Connected" + (j.account ? " as " + j.account : "")
      + ". The first sync has started. You can close this tab.";
    fetch("/api/connections/{slug}/sync", {{method: "POST",
      headers: {{"Content-Type": "application/json"}}, body: "{{}}"}}).catch(() => {{}});
  }} else {{
    h.textContent = "Could not connect {name}"; h.className = "err";
    m.textContent = (j.detail || "Unknown error") + ". ";
    const a = document.createElement("a"); a.href = "/"; a.textContent = "Back to groceries"; m.append(a);
  }}
}};
</script></body></html>"""

    @app.post("/api/connections/{slug}/disconnect")
    def disconnect(slug: str, request: Request) -> dict[str, bool]:
        check_post(request)
        if sync_lock.locked():
            raise HTTPException(409, "a sync is running; try again when it has finished")
        connections.disconnect(known(slug))
        log.info("disconnected %s", slug)
        return {"connected": False}

    @app.post("/api/connections/{slug}/sync", status_code=202)
    def sync_now(slug: str, request: Request) -> Response:
        check_post(request)
        known(slug)
        if not connections.is_connected(slug):
            raise HTTPException(409, "not connected")
        if not sync_lock.acquire(blocking=False):
            raise HTTPException(409, "a sync is already running")

        def run() -> None:
            from .sync import run_sync

            try:
                run_sync(config, slug)
            except Exception:  # noqa: BLE001  (recorded in sync_runs and alerted by run_sync)
                log.warning("sync of %s from the web UI failed", slug, exc_info=True)
            finally:
                sync_lock.release()

        threading.Thread(target=run, name=f"sync-{slug}", daemon=True).start()
        return JSONResponse({"started": True}, status_code=202)
