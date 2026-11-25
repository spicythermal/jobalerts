"""Jobs page: tabs, filters, save/dismiss/applied, each job's page, and alert links into the web UI."""
import json
import re
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from jobalerts import vault
from jobalerts.config import load_config
from jobalerts.dashboard import MqttPublisher, job_link
from jobalerts.http import HttpClient
from jobalerts.models import Job
from jobalerts.notify import Notifier
from jobalerts.scheduler import Runner
from jobalerts.store import Store
from jobalerts.web.app import create_app

from conftest import REPO, run, with_reference_home
from test_pipeline import make_job

PASSWORD = "jobs page test password"


def add(store, n, *, title="Plasma Physicist", status="alerted", score=None, step_up=None, location="Remote",
        salary="", user_state=None, board="acme"):
    job = make_job(n, title=title, location=location)
    job.board, job.salary = board, salary
    store.insert_job(job, status)
    store.update_job(job.key, status=status, score=score, step_up=step_up, score_summary=f"summary {n}",
                     red_flags="travel 30%" if n == 1 else None)
    if user_state:
        store.set_user_state(job.key, user_state)
    return job.key


@pytest.fixture
def env(tmp_path):
    cfg = with_reference_home(load_config(REPO / "config" / "config.yaml"))
    cfg.config_dir = tmp_path / "config"
    for name in ("col_data", "tax_rules", "tax_data"):
        setattr(cfg.pay, name, str(REPO / "config" / getattr(cfg.pay, name)))
    cfg.web.public_url = "http://jobs.lan:8765"
    store = Store(tmp_path / "jobs.db")
    runner = Runner(cfg, store, HttpClient(), Notifier(cfg.notify, dry_run=True))
    keys = {
        "top": add(store, 1, score=88, step_up=40, salary="$180K-220K/yr", location="Austin, TX"),
        "digest": add(store, 2, status="digest", score=60, title="Beam Physicist"),
        "low": add(store, 3, status="low", score=20, title="Plasma Technician Lead"),
        "step": add(store, 4, status="digest", score=55, step_up=85, title="Head of Physics", location="Boston, MA"),
        "gone": add(store, 5, score=90, user_state="dismissed", title="Dismissed Physicist"),
        "saved": add(store, 6, status="digest", score=70, user_state="saved", title="Saved Physicist"),
        "slash": add(store, 7, score=75, board="nvidia/External", title="Workday Physicist"),
    }
    app = create_app(runner, b"j" * 32)
    client = TestClient(app, follow_redirects=False)
    token = re.search(r'name="csrf" value="([^"]+)"', client.get("/setup").text).group(1)
    client.post("/setup", data={"csrf": token, "code": app.state.web.setup_code, "password": PASSWORD, "again": PASSWORD})
    yield runner, client, keys
    vault.install(None)
    store.close()


def titles(html):
    return re.findall(r'class="title" href="[^"]+">([^<]+)</a>', html)


def test_tabs(env):
    runner, client, keys = env
    rec = titles(client.get("/").text)
    assert rec[0] == "Plasma Physicist" and "Plasma Technician Lead" not in rec and "Dismissed Physicist" not in rec
    assert {"Beam Physicist", "Head of Physics", "Saved Physicist", "Workday Physicist"} <= set(rec)
    assert titles(client.get("/?tab=step_ups").text)[0] == "Head of Physics"
    assert titles(client.get("/?tab=saved").text) == ["Saved Physicist"]
    every = titles(client.get("/?tab=all").text)
    assert "Plasma Technician Lead" in every and "Dismissed Physicist" in every


def test_filters(env):
    runner, client, keys = env
    assert titles(client.get("/?min_score=80").text) == ["Plasma Physicist"]
    assert titles(client.get("/?q=workday").text) == ["Workday Physicist"]
    assert "Plasma Physicist" in titles(client.get("/?min_pay=1.0").text)       # has a stated salary
    assert "Beam Physicist" not in titles(client.get("/?min_pay=1.0").text)     # no pay index
    assert "Head of Physics" not in titles(client.get("/?remote=1").text)       # Boston, not remote


def test_save_dismiss_applied(env):
    runner, client, keys = env
    token = re.search(r'"X-CSRF-Token": "([^"]+)"', client.get("/").text).group(1)
    path = f"/jobs/{quote(keys['top'], safe='')}/state"
    assert client.post(path, data={"state": "saved"}).status_code == 403          # CSRF
    r = client.post(path, data={"state": "saved", "where": "list"}, headers={"X-CSRF-Token": token, "HX-Request": "true"})
    assert r.status_code == 200 and "Saved ✓" in r.text and runner.store.get_job(keys["top"])["user_state"] == "saved"
    assert runner._dashboard_stale.is_set()
    r = client.post(path, data={"state": "dismissed", "where": "list"}, headers={"X-CSRF-Token": token, "HX-Request": "true"})
    assert r.text == "" and "Plasma Physicist" not in titles(client.get("/").text)   # the card goes
    total, top = runner.top_matches()
    assert quote(keys["top"], safe="") not in [j["link"].rsplit("/", 1)[-1] for j in top]   # out of Home Assistant too
    r = client.post(path, data={"state": "clear", "where": "page"}, headers={"X-CSRF-Token": token})
    assert r.status_code == 303 and runner.store.get_job(keys["top"])["user_state"] is None
    assert client.post(path, data={"state": "bogus"}, headers={"X-CSRF-Token": token}).status_code == 400
    assert client.post("/jobs/nope/state", data={"state": "saved"}, headers={"X-CSRF-Token": token}).status_code == 404


def test_job_page(env):
    runner, client, keys = env
    html = client.get(f"/jobs/{quote(keys['top'], safe='')}").text
    assert "Plasma Physicist" in html and "summary 1" in html and "travel 30%" in html
    assert "take-home" in html and "pay index" in html          # the pay explainer, from the stated salary
    assert client.get(f"/jobs/{quote(keys['slash'], safe='')}").status_code == 200   # a Workday key with a /
    assert client.get("/jobs/fake:none:0").status_code == 404


def test_links_from_alerts_and_home_assistant(env):
    runner, client, keys = env
    link = job_link("http://jobs.lan:8765/", keys["slash"])
    assert link == "http://jobs.lan:8765/jobs/" + quote(keys["slash"], safe="")
    notifier = Notifier(runner.cfg.notify, dry_run=True)
    job = Job("fake", "acme", "9", "Acme", "Physicist", "https://acme.example/jobs/9")
    run(notifier.job_alert(job, link=link))
    run(notifier.job_alert(job))
    assert notifier.sent[0]["click"] == link and notifier.sent[0]["actions"][0]["url"] == job.url
    assert notifier.sent[1]["click"] == job.url
    total, jobs = runner.top_matches()
    attributes = json.loads(MqttPublisher(runner.cfg.homeassistant).messages(total, jobs)[1][1])
    first = attributes["jobs"][0]
    assert first["link"].startswith("http://jobs.lan:8765/jobs/") and "red_flags" not in first and "stories" not in first
    # the link only opens after logging in
    guest = TestClient(client.app, follow_redirects=False)
    assert guest.get(first["link"].replace("http://jobs.lan:8765", "")).status_code == 303
