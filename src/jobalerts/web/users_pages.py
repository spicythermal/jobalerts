"""Settings → Users (admins only; app.ADMIN_ONLY): accounts and whether users may use the install's
Anthropic key.

A new account and a reset get a one-time password, shown once on the page that made it; its user
must change it at the next login. The first admin (user 1) can't be deleted, demoted or disabled,
and there's always an enabled admin (users.Users enforces both).
"""
from __future__ import annotations

import logging
import socket

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from .. import settings as st, updater
from ..users import OWNER, ROLES, UserError, check_username, one_time_password

log = logging.getLogger(__name__)


def lan_address() -> str:
    """This computer's address on the local network (no packet is sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))     # TEST-NET: only picks the outgoing interface
            return s.getsockname()[0]
    except OSError:
        return ""


def add_user_routes(app: FastAPI, state, page, require_csrf) -> None:
    runner = state.runner
    users = state.users
    from .settings_pages import SUBNAV

    def users_page(request: Request, saved: str = "", error: str = "", made: dict | None = None,
                   errors: dict | None = None, submitted: dict | None = None):
        me = state.me(request)
        data = me.cfg.model_dump()
        rows = []
        for f in st.FIELDS:
            if f.section != "users":
                continue
            value = st.get_path(data, f.key)
            rows.append({"f": f, "value": submitted.get(f.key, "") if submitted is not None else st.format_value(f, value),
                         "checked": (f.key in submitted) if submitted is not None else bool(value),
                         "source": me.cfg.value_sources.get(f.key, "default")})
        cfg = runner.cfg
        address = cfg.web.public_url.rstrip("/")
        if not address and updater.is_packaged() and cfg.web.bind not in ("127.0.0.1", "localhost") \
                and (ip := lan_address()):     # the desktop app with web.lan on
            address = f"http://{ip}:{cfg.web.port}"
        return page(request, "users.html", users=users.all(), rows=rows, errors=errors or {}, saved=saved,
                    error=error, made=made, roles=ROLES, owner=OWNER, me_id=request.state.user.id, address=address,
                    subnav=SUBNAV, section="users")

    def change(request: Request, fn, saved: str):
        try:
            fn()
        except UserError as e:
            return users_page(request, error=str(e))
        runner.sync_people()
        return RedirectResponse(f"/settings/users?saved={saved}", 303)

    @app.get("/settings/users")
    async def list_users(request: Request, saved: str = ""):
        return users_page(request, saved)

    @app.post("/settings/users/add", dependencies=[Depends(require_csrf)])
    async def add_user(request: Request, username: str = Form(""), role: str = Form("user")):
        try:
            name = check_username(username)
            password = one_time_password()
            user = users.create(name, password, role if role in ROLES else "user", must_change=True)
        except UserError as e:
            return users_page(request, error=str(e))
        runner.sync_people()
        log.info("Web UI: %s added user %s (%s)", request.state.user.username, user.username, user.role)
        return users_page(request, made={"username": user.username, "password": password, "new": True})

    def target(uid: int):
        user = users.get(uid)
        if user is None:
            raise HTTPException(404)
        return user

    @app.post("/settings/users/{uid}/reset", dependencies=[Depends(require_csrf)])
    async def reset_password(request: Request, uid: int):
        user = target(uid)
        password = one_time_password()
        users.set_password(user.id, password, must_change=True)
        log.info("Web UI: %s reset the password of %s", request.state.user.username, user.username)
        return users_page(request, made={"username": user.username, "password": password, "new": False})

    @app.post("/settings/users/{uid}/role", dependencies=[Depends(require_csrf)])
    async def set_role(request: Request, uid: int, role: str = Form(...)):
        user = target(uid)
        return change(request, lambda: users.set_role(user.id, role), "role")

    @app.post("/settings/users/{uid}/disable", dependencies=[Depends(require_csrf)])
    async def disable(request: Request, uid: int):
        user = target(uid)
        return change(request, lambda: users.set_disabled(user.id, True), "disabled")

    @app.post("/settings/users/{uid}/enable", dependencies=[Depends(require_csrf)])
    async def enable(request: Request, uid: int):
        user = target(uid)
        return change(request, lambda: users.set_disabled(user.id, False), "enabled")

    @app.post("/settings/users/{uid}/delete", dependencies=[Depends(require_csrf)])
    async def delete(request: Request, uid: int, confirm: str = Form("")):
        user = target(uid)
        if confirm.strip().lower() != user.username:
            return users_page(request, error=f"Type {user.username} to delete that account.")
        return change(request, lambda: users.delete(user.id, runner.data_dir), "deleted")

    @app.post("/settings/users", dependencies=[Depends(require_csrf)])
    async def save_sharing(request: Request):
        from .settings_pages import validate_fields
        form = await request.form()
        submitted = {k: v for k, v in form.items() if isinstance(v, str)}
        me = state.me(request)
        errors, values, changed, _ = validate_fields(me, [f for f in st.FIELDS if f.section == "users"], submitted)
        if errors:
            return users_page(request, errors=errors, submitted=submitted)
        me.ui_settings.save(changed)
        runner.refresh()
        return RedirectResponse("/settings/users?saved=sharing", 303)
