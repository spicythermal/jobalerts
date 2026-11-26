"""The web UI: FastAPI + Jinja templates + htmx, served by `jobalerts run` (and `jobalerts web`).

Every page needs the admin login except /login, /setup, /static, /healthz and the map embed with a
valid share token (for a Home Assistant Webpage card). All handlers are `async def`, so they run on
the event loop thread that owns the SQLite connection.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import logging
import re
import secrets
import time
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ..vault import secret_key
from . import auth
from .boards_pages import add_board_routes
from .jobs_pages import add_job_routes
from .settings_pages import SUBNAV, add_settings_routes
from .stories_pages import add_story_routes

log = logging.getLogger(__name__)

HERE = Path(__file__).parent
OPEN_PATHS = ("/login", "/setup", "/healthz", "/static/", "/map/embed", "/favicon.ico")
NAV = [("/", "Jobs"), ("/boards", "Boards"), ("/map", "Map"), ("/stories", "Stories"), ("/settings", "Settings")]
META_HASH, META_VER, META_SHARE = "web.password_hash", "web.password_ver", "web.share_token"
CSP_APP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
           "connect-src 'self'; form-action 'self'; base-uri 'none'; object-src 'none'; frame-ancestors 'none'; "
           "frame-src 'self'")
# The map loads Leaflet from cdnjs, topojson + US state outlines from jsDelivr, and runs one inline
# script (its data), allowed by hash.
CSP_MAP = ("default-src 'self'; script-src https://cdnjs.cloudflare.com https://cdn.jsdelivr.net {hashes}; "
           "style-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com; img-src 'self' data: https://cdnjs.cloudflare.com; "
           "connect-src https://cdn.jsdelivr.net; form-action 'none'; base-uri 'none'; object-src 'none'; "
           "frame-ancestors {ancestors}")
_INLINE_SCRIPT = re.compile(r"<script>(.*?)</script>", re.S)


def script_hashes(html: str) -> str:
    return " ".join("'sha256-" + base64.b64encode(hashlib.sha256(s.encode()).digest()).decode() + "'"
                    for s in _INLINE_SCRIPT.findall(html))


def web_url(url: str | None) -> str:
    """A link from a job board, only if it's http(s): never `javascript:` or `data:` from a posting."""
    return url if isinstance(url, str) and url.lower().startswith(("https://", "http://")) else "#"


def safe_next(target: str | None) -> str:
    """Only same-site paths after login ("/jobs/x"), never "//evil.example" or "https://..."."""
    if not target or not target.startswith("/") or target.startswith("//") or "\\" in target:
        return "/"
    return target


class WebState:
    def __init__(self, runner, key: bytes):
        self.runner = runner
        self.store = runner.store
        self.cfg = runner.cfg
        self.signer = auth.Signer(key)
        self.limiter = auth.LoginLimiter()
        self.setup_code = ""
        if not self.password_hash():
            raw = secrets.token_hex(4).upper()
            self.setup_code = f"{raw[:4]}-{raw[4:]}"
            log.warning("Web UI has no admin password yet. Open %s/setup and enter the setup code %s",
                        self.base_url() or f"http://<this machine>:{self.cfg.web.port}", self.setup_code)
        self.test_transport = None  # tests swap in an httpx mock transport for the key Test buttons
        # The settings pages need the vault and the UI settings; `jobalerts run`/`web` set them up in
        # cli.open_runner, tests get them here.
        if runner.vault is None:
            from ..vault import Vault
            runner.vault = Vault(runner.store, key)
        if runner.ui_settings is None:
            from ..settings import UiSettings
            runner.ui_settings = UiSettings(runner.store, runner.vault)

    def base_url(self) -> str:
        return self.cfg.web.public_url.rstrip("/")

    def password_hash(self) -> str:
        return self.store.get_meta(META_HASH) or ""

    def password_version(self) -> int:
        return int(self.store.get_meta(META_VER) or 0)

    def set_password(self, password: str) -> int:
        version = self.password_version() + 1
        self.store.set_meta(META_HASH, auth.hash_password(password))
        self.store.set_meta(META_VER, str(version))
        self.setup_code = ""
        return version

    def share_token(self) -> str:
        return self.store.get_meta(META_SHARE) or ""


def create_app(runner, key: bytes) -> FastAPI:
    state = WebState(runner, key)
    cfg = state.cfg
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.web = state
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    templates.env.filters["web_url"] = web_url
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    def read_session(request: Request) -> auth.Session:
        data = state.signer.loads(request.cookies.get(auth.COOKIE, ""))
        if not data or not isinstance(data.get("csrf"), str):
            return auth.Session.new()
        session = auth.Session(csrf=data["csrf"], user=str(data.get("user") or ""),
                               ver=int(data.get("ver") or 0), exp=int(data.get("exp") or 0))
        if session.user and (session.exp < time.time() or session.ver != state.password_version()):
            session.user, session.changed = "", True  # expired, or the password changed since
        return session

    @app.middleware("http")
    async def gate(request: Request, call_next):
        session = read_session(request)
        request.state.session = session
        path = request.url.path
        open_path = any(path == p or (p.endswith("/") and path.startswith(p)) for p in OPEN_PATHS)
        if not open_path and not session.user:
            if not state.password_hash():
                response = RedirectResponse("/setup", 303)
            elif request.method == "GET" and "hx-request" not in request.headers:
                response = RedirectResponse("/login?next=" + quote(path + (f"?{request.url.query}" if request.url.query else ""), safe=""), 303)
            else:
                response = PlainTextResponse("login required", 401)
        else:
            response = await call_next(request)
        if session.changed:
            response.set_cookie(auth.COOKIE, state.signer.dumps(session.payload()), httponly=True, samesite="lax",
                                secure=cfg.web.secure_cookies, max_age=cfg.web.session_days * 86400, path="/")
        headers = response.headers
        headers.setdefault("Content-Security-Policy", CSP_APP)
        headers.setdefault("X-Content-Type-Options", "nosniff")
        headers.setdefault("Referrer-Policy", "same-origin")
        if not path.startswith("/static/"):
            headers.setdefault("Cache-Control", "no-store")
        return response

    async def require_csrf(request: Request) -> None:
        given = request.headers.get("x-csrf-token")
        if not given:
            form = await request.form()
            given = form.get("csrf")
        if not auth.tokens_match(given if isinstance(given, str) else None, request.state.session.csrf):
            raise HTTPException(403, "The form expired. Reload the page and try again.")

    def page(request: Request, name: str, **context) -> HTMLResponse:
        context.update(request=request, csrf=request.state.session.csrf, nav=NAV, path=request.url.path,
                       logged_in=bool(request.state.session.user))
        return templates.TemplateResponse(request, name, context)

    def log_in(request: Request) -> None:
        session: auth.Session = request.state.session
        session.user, session.ver = "admin", state.password_version()
        session.exp = int(time.time()) + cfg.web.session_days * 86400
        session.csrf = secrets.token_urlsafe(24)  # a new token after login (no fixation)
        session.changed = True

    # ------------------------------------------------------------------ login / setup
    @app.get("/healthz")
    async def healthz():
        return PlainTextResponse("ok\n")

    @app.get("/login")
    async def login_page(request: Request, next: str = "/"):
        if not state.password_hash():
            return RedirectResponse("/setup", 303)
        if request.state.session.user:
            return RedirectResponse(safe_next(next), 303)
        return page(request, "login.html", next=safe_next(next), error="")

    @app.post("/login", dependencies=[Depends(require_csrf)])
    async def login(request: Request, password: str = Form(""), next: str = Form("/")):
        ip = request.client.host if request.client else "?"
        if wait := state.limiter.wait(ip):
            return page(request, "login.html", next=safe_next(next),
                        error=f"Too many tries. Wait {wait} seconds and try again.")
        if not auth.check_password(password, state.password_hash()):
            state.limiter.failed(ip)
            log.warning("Web UI: failed login from %s", ip)
            return page(request, "login.html", next=safe_next(next), error="Wrong password.")
        state.limiter.succeeded(ip)
        log_in(request)
        return RedirectResponse(safe_next(next), 303)

    @app.post("/logout", dependencies=[Depends(require_csrf)])
    async def logout(request: Request):
        session = request.state.session
        session.user, session.changed = "", True
        return RedirectResponse("/login", 303)

    @app.get("/setup")
    async def setup_page(request: Request):
        if state.password_hash():
            return RedirectResponse("/login", 303)
        return page(request, "setup.html", error="")

    @app.post("/setup", dependencies=[Depends(require_csrf)])
    async def setup(request: Request, code: str = Form(""), password: str = Form(""), again: str = Form("")):
        if state.password_hash():
            return RedirectResponse("/login", 303)
        ip = request.client.host if request.client else "?"
        if wait := state.limiter.wait(ip):
            return page(request, "setup.html", error=f"Too many tries. Wait {wait} seconds and try again.")
        if not auth.tokens_match(code.strip().upper(), state.setup_code):
            state.limiter.failed(ip)
            return page(request, "setup.html", error="That's not the setup code from the log.")
        if problem := auth.password_problem(password, again):
            return page(request, "setup.html", error=problem)
        state.limiter.succeeded(ip)
        state.set_password(password)
        log.info("Web UI admin password set")
        log_in(request)
        return RedirectResponse("/", 303)

    # ------------------------------------------------------------------ pages
    @app.get("/map")
    async def map_page(request: Request):
        return page(request, "map.html", enabled=bool(runner.pay and cfg.map.enabled))

    @app.get("/map/embed")
    async def map_embed(request: Request, token: str = ""):
        logged_in = bool(request.state.session.user)
        # The share link is off until you turn it on (Settings > Account); with it, the map leaves
        # out your figures. Without a login or the token: nothing, not even that the page exists.
        by_token = not logged_in and auth.tokens_match(token, state.share_token())
        if not logged_in and not by_token:
            raise HTTPException(404)
        if not (runner.pay and cfg.map.enabled):
            return PlainTextResponse("The cost-of-living map is off (pay.enabled / map.enabled).", 404)
        html = await runner.current_map(shared=by_token)
        if by_token:
            ancestors = " ".join(cfg.web.frame_origins) or "*"
        else:
            ancestors = "'self'"
        response = HTMLResponse(html)
        response.headers["Content-Security-Policy"] = CSP_MAP.format(hashes=script_hashes(html), ancestors=ancestors)
        response.headers["Referrer-Policy"] = "no-referrer"  # keeps the token out of CDN requests
        return response

    @app.get("/settings")
    async def settings_page(request: Request):
        return RedirectResponse("/settings/situation", 303)

    def share_link(request: Request) -> str:
        if not state.share_token():
            return ""
        base = state.base_url() or str(request.base_url).rstrip("/")
        return f"{base}/map/embed?token={state.share_token()}"

    @app.get("/settings/account")
    async def account_page(request: Request, saved: str = ""):
        return page(request, "account.html", share_link=share_link(request), saved=saved, error="",
                    subnav=SUBNAV, section="account")

    @app.post("/settings/account/password", dependencies=[Depends(require_csrf)])
    async def change_password(request: Request, current: str = Form(""), password: str = Form(""),
                              again: str = Form("")):
        link = share_link(request)
        if not auth.check_password(current, state.password_hash()):
            return page(request, "account.html", share_link=link, saved="", error="The current password is wrong.",
                        subnav=SUBNAV, section="account")
        if problem := auth.password_problem(password, again):
            return page(request, "account.html", share_link=link, saved="", error=problem, subnav=SUBNAV,
                        section="account")
        state.set_password(password)
        log_in(request)  # this session stays logged in; every other one is logged out
        return RedirectResponse("/settings/account?saved=password", 303)

    @app.post("/settings/account/share-token", dependencies=[Depends(require_csrf)])
    async def rotate_share_token(request: Request):
        """Turn the Home Assistant map link on, or replace it (the old one stops working)."""
        state.store.set_meta(META_SHARE, auth.new_token())
        return RedirectResponse("/settings/account?saved=share", 303)

    @app.post("/settings/account/share-off", dependencies=[Depends(require_csrf)])
    async def share_off(request: Request):
        state.store.delete_meta(META_SHARE)
        return RedirectResponse("/settings/account?saved=share-off", 303)

    add_job_routes(app, state, page, require_csrf)
    add_board_routes(app, state, page, require_csrf)
    add_story_routes(app, state, page, require_csrf)
    add_settings_routes(app, state, page, require_csrf)

    return app


async def serve(runner) -> None:
    """Run the web UI until cancelled (a task inside `jobalerts run`, or alone for `jobalerts web`)."""
    import uvicorn
    cfg = runner.cfg.web
    app = create_app(runner, secret_key(runner.cfg))
    class Server(uvicorn.Server):
        """jobalerts handles SIGTERM/SIGINT itself (cli.stop_on_signals) and stops everything, the web
        UI included; uvicorn's own handlers would stop only the web UI and leave the polling running."""
        def capture_signals(self):
            return contextlib.nullcontext()

        def install_signal_handlers(self) -> None:   # older uvicorn
            return None

    server = Server(uvicorn.Config(app, host=cfg.bind, port=cfg.port, log_level="warning",
                                   access_log=False, lifespan="off"))
    log.info("Web UI on http://%s:%d/%s", cfg.bind, cfg.port,
             f" (phone links: {cfg.public_url})" if cfg.public_url else "")
    try:
        await server.serve()
    except asyncio.CancelledError:   # stopping: close the listening socket before going
        server.should_exit = True
        raise
