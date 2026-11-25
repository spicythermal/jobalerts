"""Settings page: parsing, encrypted personal values, live reconfigure, write-only API keys, profile, resumes."""
import re

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from jobalerts import settings as st, vault
from jobalerts.config import load_config
from jobalerts.http import HttpClient
from jobalerts.notify import Notifier
from jobalerts.scheduler import Runner
from jobalerts.store import Store
from jobalerts.web.app import create_app
from jobalerts.web.settings_pages import resume_name

from conftest import REPO, with_reference_home

KEY = b"s" * 32
PASSWORD = "settings test password"
CANARY = "sk-ant-canary-555666777"


def test_parsing_form_values():
    f = st.BY_KEY
    assert st.parse(f["pay.reference_salary"], "$155K") == 155_000
    assert st.parse(f["pay.current_home.payment"], "3,400") == 3400
    assert st.parse(f["notify.digest_time"], "7:05") == "07:05"
    assert st.parse(f["criteria.title_field"], " plasma \n\n optics\r\n") == ["plasma", "optics"]
    assert st.parse(f["criteria.allow_remote"], None) is False and st.parse(f["criteria.allow_remote"], "1") is True
    for key, bad in (("pay.reference_salary", "lots"), ("notify.digest_time", "25:00"),
                     ("notify.timezone", "Mars/Olympus"), ("scoring.push_threshold", "7.5"), ("contact_email", "me")):
        with pytest.raises(st.FieldError):
            st.parse(f[key], bad)


@pytest.fixture
def env(tmp_path, monkeypatch):
    for name in vault.SECRETS:
        monkeypatch.delenv(name, raising=False)
    cfg = with_reference_home(load_config(REPO / "config" / "config.yaml"))
    cfg.config_dir = tmp_path / "config"
    for name in ("col_data", "tax_rules", "tax_data"):
        setattr(cfg.pay, name, str(REPO / "config" / getattr(cfg.pay, name)))
    store = Store(tmp_path / "data" / "jobs.db")
    runner = Runner(cfg, store, HttpClient(), Notifier(cfg.notify, dry_run=True))
    rebuilt = []
    runner.rebuild = lambda c, s: (rebuilt.append(c) or (None, None, None))
    app = create_app(runner, KEY)
    client = TestClient(app, follow_redirects=False)
    token = csrf(client, "/setup")
    client.post("/setup", data={"csrf": token, "code": app.state.web.setup_code, "password": PASSWORD, "again": PASSWORD})
    yield runner, app, client, rebuilt, tmp_path
    vault.install(None)
    store.close()


def csrf(client, path):
    return re.search(r'name="csrf" value="([^"]+)"', client.get(path).text).group(1)


def section_form(client, section):
    """The section's form as the browser would send it, unchanged."""
    html = client.get(f"/settings/{section}").text
    form = {"csrf": csrf(client, f"/settings/{section}")}
    for name, value in re.findall(r'<input id="f-\d+" name="([^"]+)" value="([^"]*)"', html):
        form[name] = value.replace("&#34;", '"').replace("&amp;", "&")
    for name, value in re.findall(r'<textarea id="f-\d+" name="([^"]+)"[^>]*>(.*?)</textarea>', html, re.S):
        form[name] = value.replace("&#34;", '"').replace("&amp;", "&").replace("&#39;", "'")
    for name in re.findall(r'<input type="checkbox" name="([^"]+)" value="1" checked>', html):
        form[name] = "1"
    for name, options in re.findall(r'<select id="f-\d+" name="([^"]+)">(.*?)</select>', html, re.S):
        form[name] = re.search(r'<option value="([^"]+)" selected>', options).group(1)
    return form


def test_saving_a_section_applies_it_live_and_stores_only_changes(env):
    runner, app, client, rebuilt, _ = env
    form = section_form(client, "alerts")
    form["scoring.push_threshold"] = "82"
    r = client.post("/settings/alerts", data=form)
    assert r.status_code == 303 and r.headers["location"] == "/settings/alerts?saved=1"
    assert runner.cfg.scoring.push_threshold == 82 and rebuilt                 # in use without a restart
    assert runner.cfg.value_sources["scoring.push_threshold"] == "ui"
    assert set(runner.ui_settings.values()) == {"scoring.push_threshold"}   # unchanged fields stay on the defaults
    assert "Saved here" in client.get("/settings/alerts").text
    client.post("/settings/alerts/reset", data={"csrf": csrf(client, "/settings/alerts")})
    assert runner.cfg.scoring.push_threshold == 75 and runner.ui_settings.values() == {}


def test_bad_values_save_nothing_and_say_why(env):
    runner, app, client, _, _ = env
    form = section_form(client, "situation")
    form["pay.reference_salary"] = "a lot"
    form["pay.current_home.payment"] = "2500"
    r = client.post("/settings/situation", data=form)
    assert r.status_code == 200 and "Enter an amount" in r.text and "Nothing was saved" in r.text
    assert runner.ui_settings.values() == {} and runner.cfg.pay.current_home.payment == 3400
    form = section_form(client, "situation")
    form["pay.reference_location"] = "Atlantis, ZZ"
    assert "Not a place the pay index knows" in client.post("/settings/situation", data=form).text


def test_personal_values_are_encrypted_and_move_into_the_pay_index(env):
    runner, app, client, _, tmp_path = env
    form = section_form(client, "situation")
    form["pay.reference_salary"] = "123,456"
    form["pay.reference_location"] = "Boulder, CO"
    assert client.post("/settings/situation", data=form).status_code == 303
    assert runner.pay.reference_salary == 123_456 and "Boulder" in runner.pay.home.label
    runner.store.db.execute("PRAGMA wal_checkpoint(FULL)")
    raw = b"".join(p.read_bytes() for p in (tmp_path / "data").glob("jobs.db*"))
    assert b"Boulder" not in raw and b"123456" not in raw
    assert runner.ui_settings.values()["pay.reference_location"] == "Boulder, CO"


def test_personal_values_move_out_of_the_local_file(env, monkeypatch):
    runner, app, client, _, tmp_path = env
    local = tmp_path / "data" / "config.local.yaml"
    local.write_text("pay:\n  reference_salary: 155000\n  current_home: {payment: 3400}\nnotify:\n  timezone: America/Denver\n")
    monkeypatch.setenv("JOBALERTS_LOCAL_CONFIG", str(local))
    runner.base_cfg.value_sources = {"pay.reference_salary": "local", "pay.current_home.payment": "local",
                                     "notify.timezone": "local"}
    runner.reconfigure(st.apply(runner.base_cfg, {}))
    page = client.get("/settings/situation").text
    assert "Store encrypted" in page and "2 personal values" in page
    client.post("/settings/situation/encrypt-local", data={"csrf": csrf(client, "/settings/situation")})
    assert yaml.safe_load(local.read_text()) == {"notify": {"timezone": "America/Denver"}}
    assert runner.cfg.pay.reference_salary == 155_000 and runner.cfg.value_sources["pay.reference_salary"] == "ui"
    assert "Store encrypted" not in client.get("/settings/situation").text


def test_api_keys_are_write_only(env):
    runner, app, client, rebuilt, _ = env
    token = csrf(client, "/settings/keys")
    assert "Paste the key" in client.post("/settings/keys/ANTHROPIC_API_KEY", data={"csrf": token, "value": " "}).text
    n = len(rebuilt)
    r = client.post("/settings/keys/ANTHROPIC_API_KEY", data={"csrf": token, "value": CANARY})
    assert r.status_code == 303 and len(rebuilt) == n + 1 and vault.secret("ANTHROPIC_API_KEY") == CANARY
    page = client.get("/settings/keys").text
    assert CANARY not in page and 'value=""' in page and "autocomplete=\"new-password\"" in page
    assert client.post("/settings/keys/NOT_A_KEY", data={"csrf": token, "value": "x"}).status_code == 404
    client.post("/settings/keys/ANTHROPIC_API_KEY/clear", data={"csrf": token})
    assert vault.secret("ANTHROPIC_API_KEY") == ""


def test_key_test_buttons_report_without_echoing(env):
    runner, app, client, _, _ = env
    token = csrf(client, "/settings/keys")
    client.post("/settings/keys/ANTHROPIC_API_KEY", data={"csrf": token, "value": CANARY})
    seen = []

    def handler(request):
        seen.append(request.headers.get("x-api-key"))
        return httpx.Response(401, json={"error": {"message": f"invalid x-api-key {CANARY}"}})
    app.state.web.test_transport = httpx.MockTransport(handler)
    r = client.post("/settings/keys/ANTHROPIC_API_KEY/test", headers={"X-CSRF-Token": token})
    assert seen == [CANARY] and "rejected" in r.text and CANARY not in r.text
    app.state.web.test_transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"data": []}))
    assert "accepted" in client.post("/settings/keys/ANTHROPIC_API_KEY/test", headers={"X-CSRF-Token": token}).text
    assert "No ntfy topic" in client.post("/settings/keys/NTFY_TOPIC/test", headers={"X-CSRF-Token": token}).text
    assert client.post("/settings/keys/RAPIDAPI_KEY/test", headers={"X-CSRF-Token": token}).status_code == 404


def test_profile_and_resumes(env):
    runner, app, client, rebuilt, tmp_path = env
    token = csrf(client, "/settings/profile")
    client.post("/settings/profile", data={"csrf": token, "profile": "# Me\r\nWants beams."})
    assert (tmp_path / "data" / "profile.md").read_text() == "# Me\nWants beams."
    r = client.post("/settings/resumes", data={"csrf": token}, files={"file": ("../My CV (2026).md", b"Beam physicist")})
    assert r.status_code == 303 and (tmp_path / "data" / "resumes" / "My-CV-2026.md").read_text() == "Beam physicist"
    assert "My-CV-2026.md" in client.get("/settings/profile").text
    bad = client.post("/settings/resumes", data={"csrf": token}, files={"file": ("cv.exe", b"MZ")})
    assert "Upload a PDF" in bad.text
    # a path in the name never reaches the handler (no route matches), and nothing is touched
    assert client.post("/settings/resumes/..%2Fprofile.md/delete", data={"csrf": token}).status_code in (404, 405)
    assert (tmp_path / "data" / "profile.md").is_file()
    client.post("/settings/resumes/My-CV-2026.md/delete", data={"csrf": token})
    assert (tmp_path / "data" / "resumes" / ".trash" / "My-CV-2026.md").is_file()
    assert "My-CV-2026.md" not in client.get("/settings/profile").text


def test_resume_names():
    assert resume_name("C:\\Users\\me\\cv.PDF") == "C-Users-me-cv.pdf"
    assert resume_name("../../etc/cv.md") == "cv.md"
    with pytest.raises(ValueError):
        resume_name(".md")


def test_household_settings_pick_the_tax_table(env):
    runner, app, client, _, _ = env
    form = section_form(client, "situation")
    assert form["household.k401_rate"] == "6" and form["household.filing"] == "single"
    form.update({"household.filing": "joint", "household.children": "2", "household.other_dependents": "1",
                 "household.k401_rate": "6"})
    assert client.post("/settings/situation", data=form).status_code == 303
    taxes = runner.pay.taxes
    assert taxes.data["household"] == {"filing": "joint", "children": 2, "other_dependents": 1}
    assert "2 children under 17" in taxes.describe() and taxes.k401_rate == 0.06
    form = section_form(client, "situation")
    form["household.children"] = "9"
    assert "less than or equal to 4" in client.post("/settings/situation", data=form).text
