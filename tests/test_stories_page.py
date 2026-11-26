"""Stories page: list with usage, editing that keeps your file's comments, new/duplicate/delete, the
"what the AI sees" preview, safe file names, and re-scoring after a change."""
import asyncio
import json
import re
import shutil

import pytest
from fastapi.testclient import TestClient

from jobalerts import stories as st, vault
from jobalerts.config import Config
from jobalerts.http import HttpClient
from jobalerts.notify import Notifier
from jobalerts.scheduler import Runner
from jobalerts.store import Store
from jobalerts.userdata import EXAMPLES
from jobalerts.web.app import create_app

from test_pipeline import make_job

PASSWORD = "stories page test password"
COMMENTED = """---
id: gun-rf
title: Rebuilt the electron gun's RF drive
employer: Example Lab   # keep this comment
dates: 2019–2021
role: lead
skills: [RF, systems integration]
metrics: ["2x uptime"]
customer: internal      # a field of my own
---
**Situation:** The gun tripped weekly.

**Action:** I redesigned the drive. TODO: add the vendor name

**Result:** Uptime doubled.

**Talk track** Thirty seconds on the trips.
"""


@pytest.fixture
def env(tmp_path, monkeypatch):
    for name in vault.SECRETS:
        monkeypatch.delenv(name, raising=False)
    folder = tmp_path / "data" / "stories"
    shutil.copytree(EXAMPLES / "stories", folder)
    (folder / "gun-rf.md").write_text(COMMENTED)
    cfg = Config(config_dir=tmp_path / "config", database=str(tmp_path / "data" / "jobs.db"), pay={"enabled": False})
    store = Store(cfg.database)
    runner = Runner(cfg, store, HttpClient(), Notifier(cfg.notify, dry_run=True))
    rebuilt = []
    runner.rebuild = lambda c, s: (rebuilt.append(1) or (None, None, None))
    app = create_app(runner, b"s" * 32)
    with TestClient(app, follow_redirects=False) as client:
        token = re.search(r'name="csrf" value="([^"]+)"', client.get("/setup").text).group(1)
        client.post("/setup", data={"csrf": token, "code": app.state.web.setup_code, "password": PASSWORD,
                                    "again": PASSWORD})
        token = re.search(r'name="csrf" value="([^"]+)"', client.get("/stories/gun-rf").text).group(1)
        yield runner, client, token, folder, rebuilt
    store.close()


def form_of(client, story_id):
    html = client.get(f"/stories/{story_id}").text
    form = dict(re.findall(r'<input name="(\w+)" value="([^"]*)"', html))
    form.update({k: v for k, v in re.findall(r'<textarea name="(\w+)"[^>]*>(.*?)</textarea>', html, re.S)})
    for name, options in re.findall(r'<select name="(\w+)">(.*?)</select>', html, re.S):
        chosen = re.search(r'<option value="([^"]*)" selected>', options)
        form[name] = chosen.group(1) if chosen else ""
    from html import unescape
    return {k: unescape(v) for k, v in form.items()}


def test_list_shows_stories_and_how_often_they_fit(env):
    runner, client, token, folder, _ = env
    job = make_job(1)
    runner.store.insert_job(job, "alerted")
    runner.store.update_job(job.key, status="alerted", score=80, top_stories=json.dumps([["gun-rf", "Rebuilt"]]))
    html = client.get("/stories").text
    assert "Rebuilt the electron gun&#39;s RF drive" in html and ">1 jobs<" in html and "1 TODO" in html
    assert html.index("Rebuilt the electron gun") < html.index("Cut a production test")   # most used first


def test_saving_unchanged_keeps_the_file_and_its_comments(env):
    runner, client, token, folder, rebuilt = env
    r = client.post("/stories/gun-rf", data={"csrf": token, **form_of(client, "gun-rf")})
    assert r.status_code == 303 and (folder / "gun-rf.md").read_text() == COMMENTED and rebuilt


def test_editing_changes_fields_and_keeps_your_own_keys(env):
    runner, client, token, folder, _ = env
    form = form_of(client, "gun-rf")
    form.update(title="Rebuilt the gun's RF drive and halved trips", metrics="2x uptime\nhalf the trips")
    client.post("/stories/gun-rf", data={"csrf": token, **form})
    meta, body, _ = st.read_story(folder / "gun-rf.md")
    assert meta["title"].endswith("halved trips") and meta["metrics"] == ["2x uptime", "half the trips"]
    assert meta["id"] == "gun-rf" and meta["customer"] == "internal" and list(meta)[0] == "id"
    assert "**Result:** Uptime doubled." in body
    form["employer"] = ""                     # a cleared field goes away
    client.post("/stories/gun-rf", data={"csrf": token, **form})
    assert "employer" not in st.read_story(folder / "gun-rf.md")[0]
    assert "needs a title" in client.post("/stories/gun-rf", data={"csrf": token, **form, "title": ""}).text


def test_preview_is_what_the_ai_sees(env):
    runner, client, token, folder, _ = env
    html = client.post("/stories/preview", data={"csrf": token, **form_of(client, "gun-rf")}).text
    assert "role: lead" in html and "Uptime doubled" in html
    assert "TODO" not in html and "vendor name" not in html and "Thirty seconds" not in html


def test_new_duplicate_delete(env):
    runner, client, token, folder, _ = env
    r = client.post("/stories/new", data={"csrf": token, "title": "Ran the beam line night shift", "role": "lead",
                                          "body": "**Result:** No downtime."})
    assert r.headers["location"] == "/stories/ran-the-beam-line-night-shift?saved=1"
    assert st.read_story(folder / "ran-the-beam-line-night-shift.md")[0]["id"] == "ran-the-beam-line-night-shift"
    again = client.post("/stories/new", data={"csrf": token, "title": "Ran the beam line night shift"})
    assert "already a story" in again.text
    assert "a-z, 0-9" in client.post("/stories/new", data={"csrf": token, "title": "x", "id": "../evil"}).text
    r = client.post("/stories/gun-rf/duplicate", data={"csrf": token})
    assert r.headers["location"] == "/stories/gun-rf-copy?saved=copied"
    assert st.read_story(folder / "gun-rf-copy.md")[0]["title"].endswith("(copy)")
    client.post("/stories/gun-rf-copy/delete", data={"csrf": token})
    assert not (folder / "gun-rf-copy.md").exists() and (folder / ".trash" / "gun-rf-copy.md").is_file()
    assert client.get("/stories/gun-rf-copy").status_code == 404
    assert client.get("/stories/..%2F..%2Fconfig").status_code == 404
    assert st.story_path(folder, "../x") is None and st.story_path(folder, "Bad Name") is None


def test_rescore_runs_in_the_background(env, monkeypatch):
    runner, client, token, folder, _ = env
    runner.scorer = type("S", (), {"basis": "new-basis"})()
    for n in range(3):
        job = make_job(n)
        runner.store.insert_job(job, "digest")
        runner.store.update_job(job.key, status="digest", score=60, score_basis="old-basis" if n else "new-basis")
    assert runner.rescore_todo() == 2
    assert "2 jobs from the last" in client.get("/stories").text

    async def fake_rescore(boards, days=None, progress=None):
        progress(0, 2)
        await asyncio.sleep(0)
        progress(1, 2)
        return [("j1",), ("j2",)], 0
    monkeypatch.setattr(runner.me, "rescore", fake_rescore)
    html = client.post("/stories/rescore", headers={"X-CSRF-Token": token}).text
    assert "Re-scoring" in html or "Re-scored 2 jobs" in html
    client.portal.call(asyncio.sleep, 0.05)
    assert "Re-scored 2 jobs." in client.get("/stories/rescore").text
