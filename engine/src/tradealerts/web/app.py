"""The web interface: a JSON API plus a single-page app served from /static.

``build_app`` puts the shared routes together for an edition's context.
``create_app`` and ``serve`` are the self-hosted edition (``tradealerts serve``); the
hosted edition builds its own context in the tradealerts_cloud package.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
import time
from importlib import resources
from pathlib import Path
from typing import Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .. import __version__
from ..workspace import Workspace
from .auth import Auth
from .auth import router as auth_router
from .common import appearance_from_raw, err
from .context import AppContext, SelfHostContext
from .routes import backup, journal, markets, news, runs, settings, status, strategies, testing

log = logging.getLogger(__name__)

STATIC = resources.files("tradealerts") / "static"
CSP = ("default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
       "font-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
# The interface's scripts, in load order. They share one scope (classic scripts, no build step).
SCRIPTS = ["util.js", "describe.js", "editor.js", "strategies.js", "testing.js", "guided.js", "dashboard.js",
           "settings.js", "widgets.js", "markets.js", "journal.js", "portfolio.js", "news.js", "compare.js", "boot.js"]


def static_version(extra_dirs=()) -> str:
    """Changes whenever an installed file changes, so the desktop window (which caches hard)
    and browsers always pick up a new version after an update."""
    h = hashlib.sha1(__version__.encode())
    for d in (Path(str(STATIC)), *extra_dirs):
        for f in sorted(Path(d).iterdir()):
            if f.is_file():
                st = f.stat()
                h.update(f"{f.name}:{st.st_size}:{st.st_mtime_ns}".encode())
    return h.hexdigest()[:10]


def build_app(ctx: AppContext, *, authorize: Callable[[Request], Response | None],
              extra_routers=(), allowed_hosts: set[str] | None = None, csrf_exempt: tuple[str, ...] = (),
              extra_static: dict[str, Path] | None = None) -> FastAPI:
    app = FastAPI(title="Trading Alerts", version=__version__, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.ctx = ctx
    app.state.runner = ctx.runner  # older name
    app.state.shutdown = None      # older name for ctx.shutdown

    @app.middleware("http")
    async def guard(request: Request, call_next):
        path = request.url.path
        if allowed_hosts is not None and request.headers.get("host", "") not in allowed_hosts:
            return JSONResponse({"detail": "Unknown host"}, 421)
        if path.startswith("/api/"):
            if (request.method not in ("GET", "HEAD") and path not in csrf_exempt
                    and request.headers.get("x-requested-with") != "tradealerts"):
                return JSONResponse({"detail": "Request blocked: missing X-Requested-With header"}, 403)
            denied = authorize(request)
            if denied is not None:
                return denied
        response: Response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = CSP
        if path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        elif path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"  # always check for a newer copy
        return response

    @app.get("/api/session")
    def session(request: Request):
        out = ctx.session(request)
        if out.get("signed_in"):
            ws = ctx.workspace(request)
            raw = ws.raw_config()
            out["appearance"] = appearance_from_raw(raw)
            out["appearance_saved"] = isinstance(raw.get("appearance"), dict)
        return out

    @app.post("/api/shutdown")
    def shutdown():
        """Used by the desktop app when you quit it, so the engine can finish cleanly."""
        stop = app.state.shutdown or ctx.shutdown
        if not ctx.desktop or not stop:
            err(404, "Not found")
        stop()
        return {"stopping": True}

    for r in (*extra_routers, strategies.router, runs.router, testing.router, markets.router, journal.router,
              news.router, settings.router, backup.router, status.router):
        app.include_router(r)

    extra_static = extra_static or {}
    scripts = SCRIPTS[:-1] + list(ctx.extra_scripts) + SCRIPTS[-1:]

    @app.get("/", include_in_schema=False)
    def index():
        html = (Path(str(STATIC)) / "index.html").read_text(encoding="utf-8")
        v = static_version(extra_static.values())
        tags = "\n".join(f'<script src="/static/{s}?v={v}" defer></script>' for s in scripts)
        html = html.replace("<!-- scripts -->", tags)
        html = re.sub(r'(/static/[\w.-]+\.(?:js|css))(?=")', rf"\1?v={v}", html)
        return Response(html, media_type="text/html; charset=utf-8", headers={"Cache-Control": "no-store"})

    for prefix, folder in extra_static.items():
        app.mount(f"/static/{prefix}", StaticFiles(directory=str(folder)), name=f"static-{prefix}")
    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")
    return app


# --------------------------------------------------------------------------- self-hosted edition


def create_app(config_path: str | Path | None = None, password: str | None = None, runner=None,
               allowed_hosts: set[str] | None = None, desktop: bool = False) -> FastAPI:
    from .. import news

    ws = Workspace(Path(config_path) if config_path else Path("config.yaml"))
    c = ws.config()
    for sid in ws.migrate():
        log.info("%s: updated to follow Settings, Price data", sid)
    news.set_data_dir(c.data_dir)
    auth = Auth(password, c.data_dir / ".session_secret")
    ctx = SelfHostContext(ws, auth, runner=runner, desktop=desktop)

    def authorize(request: Request):
        if request.url.path in ("/api/session", "/api/login") or ctx.signed_in(request):
            return None
        return JSONResponse({"detail": "Sign in to continue"}, 401)

    return build_app(ctx, authorize=authorize, extra_routers=(auth_router(auth),), allowed_hosts=allowed_hosts)


def _parent_alive(pid: int) -> bool:
    if os.name == "nt":
        import ctypes

        k = ctypes.windll.kernel32
        handle = k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        ok = k.GetExitCodeProcess(handle, ctypes.byref(code))
        k.CloseHandle(handle)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def serve(config_path: str | None, host: str, port: int, run_live: bool = True, no_auth: bool = False,
          desktop: bool = False, parent_pid: int | None = None) -> None:
    import uvicorn

    from ..runner import LiveRunner

    password = os.environ.get("TRADEALERTS_PASSWORD") or None
    if desktop:
        host, password = "127.0.0.1", None  # the desktop app only ever talks to this computer
    loopback = host in ("127.0.0.1", "localhost", "::1")
    if no_auth:
        password = None
    elif not loopback and not password:
        raise SystemExit(
            f"Refusing to listen on {host} without a password. Set TRADEALERTS_PASSWORD, or pass "
            "--no-auth if a reverse proxy (e.g. Caddy basic auth) already protects it."
        )
    path = config_path or "config.yaml"
    runner = LiveRunner(Workspace(Path(path))).start() if run_live else None
    # On this computer only, refuse requests addressed to any other host name (DNS rebinding).
    hosts = {f"{n}:{port}" for n in ("127.0.0.1", "localhost", "[::1]")} if loopback else None
    app = create_app(path, password=password, runner=runner, allowed_hosts=hosts, desktop=desktop)
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="warning",
                                           proxy_headers=not loopback))
    app.state.ctx.shutdown = lambda: setattr(server, "should_exit", True)
    if parent_pid:
        def watch():
            while not server.should_exit:
                if not _parent_alive(parent_pid):
                    log.warning("Desktop app closed; stopping")
                    server.should_exit = True
                    return
                time.sleep(2)
        threading.Thread(target=watch, name="parent-watch", daemon=True).start()
    shown = "localhost" if loopback else host
    print(f"Trading Alerts is running at http://{shown}:{port}  (Ctrl+C to stop)", flush=True)
    if not run_live:
        print("Live checks are off (--no-runner).")
    try:
        server.run()
    finally:
        if runner:
            runner.stop()
