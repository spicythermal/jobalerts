"""The web UI: FastAPI + Jinja templates + htmx, served by `jobalerts run` (and `jobalerts web`).

Every page needs a login except /login, /setup, /static, /healthz and the map embed with a valid
share token (for a Home Assistant Webpage card). Each user sees only their own jobs, filter, stories,
profile, settings and keys: pages get the logged-in user's pipeline (`state.me(request)`), never a
user id from the request. Admin pages (users, connections, updates, the install's keys, board
editing) answer 403 to everyone else (ADMIN_ONLY). All handlers are `async def`, so they run on the
event loop thread that owns the SQLite connection.
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

from ..models import web_url
from ..users import UserError, Users, check_username
from ..vault import secret_key
from . import auth
from .boards_pages import add_board_routes
from .filter_pages import add_filter_routes
from .jobs_pages import add_job_routes
from .settings_pages import SUBNAV, add_settings_routes
from .stories_pages import add_story_routes
from .users_pages import add_user_routes

log = logging.getLogger(__name__)

HERE = Path(__file__).parent
OPEN_PATHS = ("/login", "/setup", "/healthz", "/static/", "/map/embed", "/favicon.ico")
NAV = [("/", "Jobs"), ("/boards", "Boards"), ("/filter", "Filter"), ("/map", "Map"), ("/stories", "Stories"), ("/settings", "Settings")]
META_SHARE = "web.share_token"      # per user (store.USER_META)
# Admins only: accounts, the install's connections, updates and keys, and editing the watch list.
ADMIN_ONLY = ("/settings/users", "/settings/connections", "/settings/updates", "/settings/keys/install")
# While a user must change the password an admin gave them, only these answer.
MUST_CHANGE_OK = ("/settings/account", "/settings/account/password", "/logout")
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


def safe_next(target: str | None) -> str:
    """Only same-site paths after login ("/jobs/x"), never "//evil.example" or "https://..."."""
    if not target or not target.startswith("/") or target.startswith("//") or "\\" in target:
        return "/"
    return target


def admin_only(method: str, path: str) -> bool:
    return any(path == p or path.startswith(p + "/") for p in ADMIN_ONLY) or (method != "GET" and path.startswith("/boards"))


class WebState:
    def __init__(self, runner, key: bytes):
        self.runner = runner
        self.store = runner.store
        self.cfg = runner.cfg
        self.signer = auth.Signer(key)
        self.limiter = auth.LoginLimiter()
        self.setup_code = ""
        self.test_transport = None  # tests swap in an httpx mock transport for the key Test buttons
        # The settings pages need the vault, the UI settings and the accounts; `jobalerts run`/`web` set
        # them up in cli.open_runner, tests get them here.
        if runner.vault is None:
            from ..vault import Vault
            runner.vault = Vault(runner.store, key)
        if runner.ui_settings is None:
            from ..settings import UiSettings
            runner.ui_settings = UiSettings(runner.store, runner.vault)
        if runner.users is None:
            runner.users = Users(runner.store)
        self.users: Users = runner.users
        if not self.users.any():
            raw = secrets.token_hex(4).upper()
            # the desktop app makes the code itself and opens the setup page with it filled in
            self.setup_code = self.cfg.web.setup_code.upper() or f"{raw[:4]}-{raw[4:]}"
            log.warning("Web UI has no admin account yet. Open %s/setup and enter the setup code %s",
                        self.base_url() or f"http://<this machine>:{self.cfg.web.port}", self.setup_code)

    def base_url(self) -> str:
        return self.cfg.web.public_url.rstrip("/")

    def me(self, request: Request):
        """The logged-in user's pipeline (Person): their store view, settings, filter, scorer."""
        user = request.state.user
        person = self.runner.person(user.id)
        if person is None:
            self.runner.sync_people()
            person = self.runner.person(user.id)
        if person is None:
            raise HTTPException(403)
        return person

    def share_token(self, person=None) -> str:
        """A user's map share token (default: the first user's); "" when their link is off."""
        return (person or self.runner.me).store.get_meta(META_SHARE) or ""


def create_app(runner, key: bytes) -> FastAPI:
    state = WebState(runner, key)
    cfg = state.cfg
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.web = state
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    templates.env.filters["web_url"] = web_url
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    def read_session(request: Request) -> tuple[auth.Session, object]:
        data = state.signer.loads(request.cookies.get(auth.COOKIE, ""))
        if not data or not isinstance(data.get("csrf"), str):
            return auth.Session.new(), None
        try:
            session = auth.Session(csrf=data["csrf"], uid=int(data.get("uid") or 0), ver=int(data.get("ver") or 0),
                                   exp=int(data.get("exp") or 0), sid=str(data.get("sid") or ""))
        except (TypeError, ValueError):
            return auth.Session.new(), None
        user = state.users.get(session.uid) if session.uid else None
        if session.uid and (user is None or user.disabled or session.exp < time.time() or session.ver != user.ver
                            or (session.sid and state.users.session_ended(session.sid))):
            session.uid, session.changed, user = 0, True, None  # expired, removed, logged out, or the password or role changed
        elif session.uid and not session.sid:   # a login from before session ids: give it one
            session.sid, session.changed = secrets.token_urlsafe(16), True
        return session, user

    @app.middleware("http")
    async def gate(request: Request, call_next):
        session, user = read_session(request)
        request.state.session, request.state.user = session, user
        path = request.url.path
        open_path = any(path == p or (p.endswith("/") and path.startswith(p)) for p in OPEN_PATHS)
        if not open_path and user is None:
            if not state.users.any():
                response = RedirectResponse("/setup", 303)
            elif request.method == "GET" and "hx-request" not in request.headers:
                response = RedirectResponse("/login?next=" + quote(path + (f"?{request.url.query}" if request.url.query else ""), safe=""), 303)
            else:
                response = PlainTextResponse("login required", 401)
        elif user is not None and user.must_change and not open_path and path not in MUST_CHANGE_OK:
            response = (RedirectResponse("/settings/account?must=1", 303) if request.method == "GET"
                        else PlainTextResponse("change your password first", 403))
        elif user is not None and not user.admin and admin_only(request.method, path):
            response = PlainTextResponse("admins only", 403)
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
        user = getattr(request.state, "user", None)
        context.update(request=request, csrf=request.state.session.csrf, nav=NAV, path=request.url.path,
                       logged_in=user is not None, user=user, is_admin=bool(user and user.admin))
        if "subnav" in context and not (user and user.admin):
            context["subnav"] = [(k, label) for k, label in context["subnav"] if not admin_only("GET", f"/settings/{k}")]
        return templates.TemplateResponse(request, name, context)

    def log_in(request: Request, user) -> None:
        session: auth.Session = request.state.session
        session.uid, session.ver = user.id, user.ver
        session.exp = int(time.time()) + cfg.web.session_days * 86400
        session.csrf = secrets.token_urlsafe(24)  # a new token after login (no fixation)
        session.sid = secrets.token_urlsafe(16)
        session.changed = True
        request.state.user = user

    # ------------------------------------------------------------------ login / setup
    @app.get("/healthz")
    async def healthz():
        return PlainTextResponse("ok\n")

    @app.get("/login")
    async def login_page(request: Request, next: str = "/"):
        if not state.users.any():
            return RedirectResponse("/setup", 303)
        if request.state.user:
            return RedirectResponse(safe_next(next), 303)
        return page(request, "login.html", next=safe_next(next), error="", username="")

    @app.post("/login", dependencies=[Depends(require_csrf)])
    async def login(request: Request, username: str = Form(""), password: str = Form(""), next: str = Form("/")):
        ip = request.client.host if request.client else "?"
        name = username.strip().lower()[:64]
        if wait := max(state.limiter.wait(ip), state.limiter.wait("user:" + name)):
            return page(request, "login.html", next=safe_next(next), username=name,
                        error=f"Too many tries. Wait {wait} seconds and try again.")
        user = state.users.authenticate(name, password)
        if user is None:
            state.limiter.failed(ip)
            state.limiter.failed("user:" + name)
            log.warning("Web UI: failed login from %s", ip)
            return page(request, "login.html", next=safe_next(next), username=name,
                        error="Wrong username or password.")
        state.limiter.succeeded(ip)
        state.limiter.succeeded("user:" + name)
        log_in(request, user)
        return RedirectResponse("/settings/account?must=1" if user.must_change else safe_next(next), 303)

    @app.post("/logout", dependencies=[Depends(require_csrf)])
    async def logout(request: Request):
        session = request.state.session
        if session.uid and session.sid:
            state.users.end_session(session.sid, session.exp)   # a copy of this cookie stops working too
        session.uid, session.sid, session.changed = 0, "", True
        return RedirectResponse("/login", 303)

    @app.get("/setup")
    async def setup_page(request: Request, code: str = ""):
        if state.users.any():
            return RedirectResponse("/login", 303)
        return page(request, "setup.html", error="", code=code[:20], username="admin", share="no")

    @app.post("/setup", dependencies=[Depends(require_csrf)])
    async def setup(request: Request, code: str = Form(""), username: str = Form("admin"), password: str = Form(""),
                    again: str = Form(""), share: str = Form("no")):
        if state.users.any():
            return RedirectResponse("/login", 303)
        ip = request.client.host if request.client else "?"

        def again_page(error: str):
            return page(request, "setup.html", error=error, code="", username=username[:32], share=share)
        if wait := state.limiter.wait(ip):
            return again_page(f"Too many tries. Wait {wait} seconds and try again.")
        if not auth.tokens_match(code.strip().upper(), state.setup_code):
            state.limiter.failed(ip)
            return again_page("That's not the setup code from the log.")
        try:
            name = check_username(username)
        except UserError as e:
            return again_page(str(e))
        if problem := auth.password_problem(password, again):
            return again_page(problem)
        state.limiter.succeeded(ip)
        user = state.users.create(name, password, role="admin", must_change=False)
        state.setup_code = ""
        if share == "yes":     # off is the default
            runner.ui_settings.save({"users.share_keys": True})
        runner.refresh()
        log.info("Web UI: admin account %s created", name)
        log_in(request, user)
        return RedirectResponse("/", 303)

    # ------------------------------------------------------------------ pages
    @app.get("/map")
    async def map_page(request: Request):
        return page(request, "map.html", enabled=bool(state.me(request).pay and cfg.map.enabled))

    @app.get("/map/embed")
    async def map_embed(request: Request, token: str = ""):
        logged_in = request.state.user is not None
        # The share link is off until you turn it on (Settings > Account); with it, the map leaves
        # out your figures. Without a login or the token: nothing, not even that the page exists.
        person = state.me(request) if logged_in else next(
            (p for p in runner.everyone() if auth.tokens_match(token, state.share_token(p))), None)
        by_token = not logged_in and person is not None
        if person is None:
            raise HTTPException(404)
        if not (person.pay and cfg.map.enabled):
            return PlainTextResponse("The cost-of-living map is off (pay.enabled / map.enabled).", 404)
        html = await person.current_map(shared=by_token)
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
        token = state.share_token(state.me(request))
        if not token:
            return ""
        base = state.base_url() or str(request.base_url).rstrip("/")
        return f"{base}/map/embed?token={token}"

    def account(request: Request, saved: str = "", error: str = "", must: bool = False):
        return page(request, "account.html", share_link=share_link(request), saved=saved, error=error,
                    must=must or request.state.user.must_change, subnav=SUBNAV, section="account")

    @app.get("/settings/account")
    async def account_page(request: Request, saved: str = "", must: str = ""):
        return account(request, saved, must=bool(must))

    @app.post("/settings/account/password", dependencies=[Depends(require_csrf)])
    async def change_password(request: Request, current: str = Form(""), password: str = Form(""),
                              again: str = Form("")):
        user = request.state.user
        if not state.users.password_ok(user.id, current):
            return account(request, error="The current password is wrong.")
        if problem := auth.password_problem(password, again):
            return account(request, error=problem)
        if password == current:
            return account(request, error="Choose a new password, not the one you have.")
        state.users.set_password(user.id, password)
        log_in(request, state.users.get(user.id))  # this session stays logged in; every other one is logged out
        return RedirectResponse("/settings/account?saved=password", 303)

    @app.post("/settings/account/share-token", dependencies=[Depends(require_csrf)])
    async def rotate_share_token(request: Request):
        """Turn the Home Assistant map link on, or replace it (the old one stops working)."""
        state.me(request).store.set_meta(META_SHARE, auth.new_token())
        return RedirectResponse("/settings/account?saved=share", 303)

    @app.post("/settings/account/share-off", dependencies=[Depends(require_csrf)])
    async def share_off(request: Request):
        state.me(request).store.delete_meta(META_SHARE)
        return RedirectResponse("/settings/account?saved=share-off", 303)

    add_job_routes(app, state, page, require_csrf)
    add_board_routes(app, state, page, require_csrf)
    add_filter_routes(app, state, page, require_csrf)
    add_story_routes(app, state, page, require_csrf)
    add_user_routes(app, state, page, require_csrf)
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
