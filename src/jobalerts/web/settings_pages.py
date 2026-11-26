"""The Settings pages: your situation, search words, scoring and alerts, API keys (write-only),
profile and resumes are each user's own; connections, updates and the install's keys are the admins'
(app.ADMIN_ONLY). Account (password, share link) lives in app.py, users in users_pages.py.

API keys never travel back to the browser: the keys page shows only whether each one is set and
where from. A failed save re-renders the form without the value that was typed.
"""
from __future__ import annotations

import logging
import re
import shutil
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse
from pydantic import ValidationError

from .. import settings as st, updater
from ..resumes import load_resumes
from ..userdata import local_config_path
from ..users import OWNER
from ..vault import INSTALL, INSTALL_SECRETS, USER_SECRETS, _redactor, install as install_vault

log = logging.getLogger(__name__)

SUBNAV = [("situation", "Your situation"), ("search", "Filter (own page)"), ("alerts", "Scoring and alerts"),
          ("keys", "API keys"), ("profile", "Profile and resumes"), ("account", "Account"),
          ("connections", "Connections"), ("updates", "Updates"), ("users", "Users")]
RESUME_TYPES = {".pdf", ".md", ".txt"}
MAX_RESUME = 5 * 1024 * 1024
NO_TEST = ("No test button: each call uses paid credits or a small trial allowance. The Boards page shows "
           "whether the next poll worked.")


def resume_name(filename: str) -> str:
    """A safe file name for an upload: letters, digits, dot, dash, underscore; no folders."""
    name = Path(filename or "").name
    stem, suffix = Path(name).stem, Path(name).suffix.lower()
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip(".-")[:80]
    if not stem or suffix not in RESUME_TYPES:
        raise ValueError("Upload a PDF, Markdown (.md) or text (.txt) resume.")
    return stem + suffix


def my_keys(person) -> tuple[str, ...]:
    """The keys a user sets for themselves (user 1's Anthropic key is the install's)."""
    return tuple(n for n in USER_SECRETS if not (person.uid == OWNER and n == "ANTHROPIC_API_KEY"))


def validate_fields(person, fields: list[st.Field], submitted: dict[str, str]):
    """Check a form of these settings against the user's whole config. Returns (errors by key, parsed
    values, the values to save (those that differ from the files, or were saved before), the new config
    or None)."""
    errors, values = {}, {}
    for f in fields:
        try:
            values[f.key] = st.parse(f, submitted.get(f.key))
        except st.FieldError as e:
            errors[f.key] = str(e)
    base_cfg, ui = person.base_cfg, person.ui_settings
    base = base_cfg.model_dump()
    current_ui = ui.values()
    changed = {k: v for k, v in values.items() if k in current_ui or st.get_path(base, k) != v}
    new_cfg = None
    if not errors:
        overlay = st.deep_merge(ui.overlay(), {})
        for key, value in changed.items():
            overlay = st.deep_merge(overlay, st.nest(key, value))
        try:
            new_cfg = st.apply(base_cfg, overlay, strict=True)
        except ValidationError as e:
            errors.update({k: v for k, v in st.errors_by_field(e).items()} or {"": str(e)})
    return errors, values, changed, new_cfg


def add_settings_routes(app: FastAPI, state, page, require_csrf) -> None:
    runner = state.runner

    def context(request: Request, section: str, **extra):
        me = state.me(request)
        problems = me.cfg.config_problems + [
            f"{k}: saved on the Settings page by another version; not used by this one (kept)"
            for k in me.ui_settings.unknown()]
        return dict(subnav=SUBNAV, section=section, problems=problems, **extra)

    def field_rows(section: str, cfg, form: dict | None = None):
        data = cfg.model_dump()
        rows = []
        for f in st.FIELDS:
            if f.section != section:
                continue
            value = st.get_path(data, f.key) if form is None else None
            shown = (form.get(f.key, "") if form is not None and f.kind != "bool" else st.format_value(f, value))
            checked = bool(value) if form is None else f.key in form
            source = cfg.value_sources.get(f.key, "default")
            rows.append({"f": f, "value": shown, "checked": checked, "source": source})
        return rows

    def local_sensitive(person) -> list[str]:
        """Personal values from config.local.yaml (user 1's file) on the situation page."""
        if person.uid != OWNER:
            return []
        return [f.key for f in st.FIELDS if f.sensitive and f.section == "situation"
                and person.cfg.value_sources.get(f.key) == "local"]

    def changed_settings(person, scope: str) -> None:
        """Use the new settings now: the install's change everyone's, a user's only theirs."""
        runner.refresh(None if scope == "install" else person.uid)
        _redactor.refresh()

    # ------------------------------------------------------------------ API keys (write-only)
    def keys_page(request: Request, saved: str, error: str = "", error_name: str = ""):
        me = state.me(request)
        mine = [{"s": s, "testable": s.name in TESTS, "scope": "mine"}
                for s in runner.vault.status(me.uid, my_keys(me))]
        install = ([{"s": s, "testable": s.name in TESTS, "scope": "install"}
                    for s in runner.vault.status(INSTALL, INSTALL_SECRETS)] if request.state.user.admin else [])
        return page(request, "settings_keys.html", **context(
            request, "keys", mine=mine, install=install, saved=saved, error=error, error_name=error_name,
            no_test=NO_TEST, owner=me.uid == OWNER, key_source=me.key_source, share=me.cfg.users.share_keys))

    def key_owner(request: Request, scope: str, name: str) -> int:
        """Whose key a form changes; 404 for a key that isn't the user's to set."""
        me = state.me(request)
        if scope == "mine" and name in my_keys(me):
            return me.uid
        if scope == "install" and name in INSTALL_SECRETS and request.state.user.admin:
            return INSTALL
        raise HTTPException(404)

    @app.post("/settings/keys/{scope}/{name}", dependencies=[Depends(require_csrf)])
    async def save_key(request: Request, scope: str, name: str, value: str = Form("")):
        uid = key_owner(request, scope, name)
        if not value.strip():
            return keys_page(request, "", "Paste the key, then Save.", name)
        runner.vault.set(name, value, uid)
        install_vault(runner.vault)
        changed_settings(state.me(request), "install" if uid == INSTALL else "user")
        return RedirectResponse(f"/settings/keys?saved={name}#{scope}-{name}", 303)

    @app.post("/settings/keys/{scope}/{name}/clear", dependencies=[Depends(require_csrf)])
    async def clear_key(request: Request, scope: str, name: str):
        uid = key_owner(request, scope, name)
        runner.vault.clear(name, uid)
        install_vault(runner.vault)
        changed_settings(state.me(request), "install" if uid == INSTALL else "user")
        return RedirectResponse(f"/settings/keys?saved=cleared#{scope}-{name}", 303)

    @app.post("/settings/keys/{scope}/{name}/test", dependencies=[Depends(require_csrf)])
    async def test_key(request: Request, scope: str, name: str):
        uid = key_owner(request, scope, name)
        if name not in TESTS:
            raise HTTPException(404)
        try:
            ok, message = await TESTS[name](state.me(request), runner, state.test_transport, uid)
        except Exception as e:  # the details (addresses, server answers) go to the log, not the page
            log.warning("Test of %s failed: %s: %s", name, type(e).__name__, e)
            ok, message = False, f"The test failed ({type(e).__name__}). The log has the details."
        message = _redactor.redact(message)
        return page(request, "key_test.html", ok=ok, message=message)

    # ------------------------------------------------------------------ updates (admins)
    def updates_page(request: Request, saved: str, message: str = ""):
        return page(request, "settings_updates.html", **context(
            request, "updates", status=runner.update_status(), rows=field_rows("updates", state.me(request).cfg),
            errors={}, saved=saved, message=message, running=runner.running, packaged=updater.is_packaged()))

    @app.post("/settings/updates/check", dependencies=[Depends(require_csrf)])
    async def check_now(request: Request):
        await runner.check_updates(act=False)
        return RedirectResponse("/settings/updates?saved=checked", 303)

    @app.post("/settings/updates/install", dependencies=[Depends(require_csrf)])
    async def update_now(request: Request):
        ok, message = await runner.install_update()
        if ok:
            message += (". Restarting into it now; reload this page in a minute." if runner.running
                        else ". Restart jobalerts to use it.")
        return updates_page(request, "", message)

    # ------------------------------------------------------------------ profile and resumes (each user's)
    def profile_path(person) -> Path:
        return person.cfg.resolve(person.cfg.scoring.profile_path)

    def resume_dir(person) -> Path:
        return person.cfg.resolve(person.cfg.scoring.resume_dir)

    def profile_page(request: Request, saved: str, error: str = ""):
        me = state.me(request)
        path = profile_path(me)
        resumes = [{"name": r.name, "chars": len(r.text)} for r in load_resumes(resume_dir(me))]
        return page(request, "settings_profile.html", **context(
            request, "profile", profile=path.read_text(encoding="utf-8") if path.is_file() else "", resumes=resumes,
            saved=saved, error=error))

    @app.post("/settings/profile", dependencies=[Depends(require_csrf)])
    async def save_profile(request: Request, profile: str = Form("")):
        me = state.me(request)
        path = profile_path(me)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(profile.replace("\r\n", "\n"), encoding="utf-8")
        tmp.replace(path)
        changed_settings(me, "user")
        return RedirectResponse("/settings/profile?saved=profile", 303)

    @app.post("/settings/resumes", dependencies=[Depends(require_csrf)])
    async def upload_resume(request: Request, file: UploadFile):
        me = state.me(request)
        try:
            name = resume_name(file.filename or "")
        except ValueError as e:
            return profile_page(request, "", str(e))
        data = await file.read(MAX_RESUME + 1)
        if len(data) > MAX_RESUME:
            return profile_page(request, "", "That file is over 5 MB.")
        folder = resume_dir(me)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / name).write_bytes(data)
        changed_settings(me, "user")
        return RedirectResponse("/settings/profile?saved=resume", 303)

    @app.post("/settings/resumes/{name}/delete", dependencies=[Depends(require_csrf)])
    async def delete_resume(request: Request, name: str):
        me = state.me(request)
        folder = resume_dir(me)
        target = folder / Path(name).name
        if Path(name).name != name or not target.is_file():
            raise HTTPException(404)
        trash = folder / ".trash"
        trash.mkdir(exist_ok=True)
        shutil.move(str(target), trash / name)  # kept, not deleted: these may be your working files
        changed_settings(me, "user")
        return RedirectResponse("/settings/profile?saved=removed", 303)

    # ------------------------------------------------------------------ field sections
    @app.get("/settings/{section}")
    async def section_page(request: Request, section: str, saved: str = ""):
        if section == "keys":
            return keys_page(request, saved)
        if section == "profile":
            return profile_page(request, saved)
        if section == "updates":
            return updates_page(request, saved)
        if section == "search":   # the filter has its own page
            return RedirectResponse("/filter" + (f"?saved={saved}" if saved else ""), 303)
        if section not in st.SECTIONS or section == "users":
            raise HTTPException(404)
        me = state.me(request)
        return page(request, "settings_section.html", **context(
            request, section, title=st.SECTIONS[section], rows=field_rows(section, me.cfg), errors={}, saved=saved,
            local_sensitive=local_sensitive(me) if section == "situation" else []))

    @app.post("/settings/{section}", dependencies=[Depends(require_csrf)])
    async def save_section(request: Request, section: str):
        if section not in st.SECTIONS or section == "users":
            raise HTTPException(404)
        me = state.me(request)
        form = await request.form()
        submitted = {k: v for k, v in form.items() if isinstance(v, str)}
        errors, values, changed, new_cfg = validate_fields(me, [f for f in st.FIELDS if f.section == section],
                                                           submitted)
        if not errors and "pay.reference_location" in values and me.pay \
                and me.pay.resolve(values["pay.reference_location"]) is None:
            errors["pay.reference_location"] = "Not a place the pay index knows. Try \"City, ST\", a state or a country."
        if errors:
            return page(request, "settings_section.html", **context(
                request, section, title=st.SECTIONS[section], rows=field_rows(section, me.cfg, submitted),
                errors=errors, saved="", local_sensitive=[]))
        me.ui_settings.save(changed)
        changed_settings(me, st.SCOPE[section])
        return RedirectResponse(f"/settings/{section}?saved=1", 303)

    @app.post("/settings/{section}/reset", dependencies=[Depends(require_csrf)])
    async def reset_section(request: Request, section: str):
        if section not in st.SECTIONS:
            raise HTTPException(404)
        me = state.me(request)
        me.ui_settings.reset([f.key for f in st.FIELDS if f.section == section])
        changed_settings(me, st.SCOPE[section])
        return RedirectResponse(f"/settings/{'users' if section == 'users' else section}?saved=reset", 303)

    @app.post("/settings/situation/encrypt-local", dependencies=[Depends(require_csrf)])
    async def encrypt_local(request: Request):
        """Personal values from config.local.yaml (plain text) -> encrypted storage, and out of the file."""
        me = state.me(request)
        keys = local_sensitive(me)
        if not keys:
            return RedirectResponse("/settings/situation", 303)
        data = me.cfg.model_dump()
        me.ui_settings.save({k: st.get_path(data, k) for k in keys})
        removed = st.move_out_of_local_file(local_config_path(runner.base_cfg.config_dir / "config.yaml"), keys)
        runner.base_cfg.value_sources = {k: v for k, v in runner.base_cfg.value_sources.items() if k not in removed}
        changed_settings(me, "user")
        log.info("Moved %d personal settings from config.local.yaml into encrypted storage", len(removed))
        return RedirectResponse("/settings/situation?saved=encrypted", 303)


# ---------------------------------------------------------------------- key tests
# (person, runner, transport, uid whose key: 0 = the install's) -> (ok, message)
async def _anthropic(person, runner, transport, uid):
    from ..vault import secret
    key = secret("ANTHROPIC_API_KEY") if uid == INSTALL else secret("ANTHROPIC_API_KEY", uid)
    async with httpx.AsyncClient(timeout=15, transport=transport) as client:
        r = await client.get("https://api.anthropic.com/v1/models", params={"limit": 1},
                             headers={"x-api-key": key, "anthropic-version": "2023-06-01"})
    if r.status_code == 200:
        return True, "Anthropic accepted the key."
    return False, f"Anthropic answered {r.status_code}: {'the key was rejected' if r.status_code in (401, 403) else r.text[:200]}"


async def _ntfy(person, runner, transport, uid):
    if person.notifier.dry_run:
        return False, "No ntfy topic set (or this is a dry run)."
    await person.notifier.send("Job alerts test", "Sent from the Settings page. Notifications work.", priority=2,
                               tags=["white_check_mark"])
    return True, "Sent a test notification; check your phone."


async def _mqtt(person, runner, transport, uid):
    if not runner.homeassistant:
        return False, "Home Assistant publishing is off or has no MQTT host (Connections)."
    total = await runner.publish_dashboard()
    return True, f"Published sensor.job_alerts ({total} matches)."


async def _usajobs(person, runner, transport, uid):
    from ..vault import secret
    async with httpx.AsyncClient(timeout=15, transport=transport) as client:
        r = await client.get("https://data.usajobs.gov/api/search", params={"Keyword": "engineer", "ResultsPerPage": 1},
                             headers={"Authorization-Key": secret("USAJOBS_API_KEY"), "User-Agent": secret("USAJOBS_EMAIL"),
                                      "Host": "data.usajobs.gov"})
    if r.status_code == 200:
        return True, "USAJobs accepted the key and email."
    return False, f"USAJobs answered {r.status_code}."


TESTS = {"ANTHROPIC_API_KEY": _anthropic, "NTFY_TOPIC": _ntfy, "NTFY_TOKEN": _ntfy, "MQTT_PASSWORD": _mqtt,
         "USAJOBS_API_KEY": _usajobs, "USAJOBS_EMAIL": _usajobs}
