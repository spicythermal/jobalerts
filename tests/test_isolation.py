"""Two users on one install see nothing of each other: every page, form, job page, story, resume,
key, the map, the share link, the digest and Home Assistant are checked for the other user's canaries."""
import re

import pytest
from fastapi.testclient import TestClient

from jobalerts import vault
from jobalerts.config import load_config
from jobalerts.dashboard import MqttPublisher
from jobalerts.http import HttpClient
from jobalerts.notify import Notifier
from jobalerts.scheduler import Runner
from jobalerts.store import Store
from jobalerts.web.app import create_app
from jobalerts.web.settings_pages import SUBNAV

from conftest import REPO, run, with_reference_home
from test_pipeline import make_job

PASSWORD = "correct horse battery"
CANARIES = {
    "admin": {"salary": "741,852", "profile": "canary-admin-profile-q8", "story": "Canary admin story z1",
              "resume": "canary-admin-resume", "topic": "canary-admin-topic-77", "summary": "canary-admin-summary-k2"},
    "alex": {"salary": "963,258", "profile": "canary-alex-profile-w4", "story": "Canary alex story y3",
             "resume": "canary-alex-resume", "topic": "canary-alex-topic-55", "summary": "canary-alex-summary-m6",
             "key": "sk-canary-alex-anthropic-9x"},
}


def csrf_of(client, path="/settings/account"):
    return re.search(r'name="csrf" value="([^"]+)"', client.get(path).text).group(1)


def fill_in(client, who: str) -> str:
    """Give `who` their own salary, profile, story, resume and ntfy topic through the web UI."""
    c = CANARIES[who]
    token = csrf_of(client)
    salary = c["salary"].replace(",", "")
    r = client.post("/settings/situation", data={"csrf": token, "pay.reference_salary": salary,
                                                 "pay.reference_location": "Denver, CO", "intent.move": "open",
                                                 "pay.move_min_index": "1.15", "household.filing": "joint",
                                                 "household.children": "1", "household.other_dependents": "0",
                                                 "household.k401_rate": "6", "pay.current_home.payment": "2000",
                                                 "pay.current_home.principal": "500", "pay.current_home.upkeep": "300",
                                                 "pay.current_home.rate": "5"})
    assert r.status_code == 303, r.text[:2000]
    assert client.post("/settings/profile", data={"csrf": token, "profile": c["profile"]}).status_code == 303
    r = client.post("/stories/new", data={"csrf": token, "title": c["story"], "body": "Did a thing.", "role": "lead"})
    assert r.status_code == 303
    files = {"file": (c["resume"] + ".md", b"# Resume\nPhysics.", "text/markdown")}
    assert client.post("/settings/resumes", data={"csrf": token}, files=files).status_code == 303
    assert client.post("/settings/keys/mine/NTFY_TOPIC", data={"csrf": token, "value": c["topic"]}).status_code == 303
    return r.headers["location"].split("?")[0].removeprefix("/stories/")


@pytest.fixture
def two(tmp_path, monkeypatch):
    for name in vault.SECRETS:
        monkeypatch.delenv(name, raising=False)
    cfg = with_reference_home(load_config(REPO / "config" / "config.yaml"))
    cfg.config_dir = tmp_path / "config"
    for name in ("col_data", "tax_rules", "tax_data"):
        setattr(cfg.pay, name, str(REPO / "config" / getattr(cfg.pay, name)))
    cfg.scoring.profile_path = str(tmp_path / "data" / "profile.md")
    cfg.scoring.stories_dir = str(tmp_path / "data" / "stories")
    cfg.scoring.resume_dir = str(tmp_path / "data" / "resumes")
    store = Store(tmp_path / "data" / "jobs.db")
    sent = []
    runner = Runner(cfg, store, HttpClient(), Notifier(cfg.notify, dry_run=True),
                    homeassistant=MqttPublisher(cfg.homeassistant, send=lambda c, messages: sent.extend(messages)))
    app = create_app(runner, b"i" * 32)
    admin = TestClient(app, follow_redirects=False)
    admin.post("/setup", data={"csrf": csrf_of(admin, "/setup"), "code": app.state.web.setup_code,
                               "password": PASSWORD, "again": PASSWORD})
    html = admin.post("/settings/users/add", data={"csrf": csrf_of(admin), "username": "alex", "role": "user"}).text
    once = re.search(r'value="([0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4})"', html).group(1)
    alex = TestClient(app, follow_redirects=False)
    alex.post("/login", data={"csrf": csrf_of(alex, "/login"), "username": "alex", "password": once})
    alex.post("/settings/account/password", data={"csrf": csrf_of(alex), "current": once, "password": PASSWORD,
                                                  "again": PASSWORD})
    stories = {"admin": fill_in(admin, "admin"), "alex": fill_in(alex, "alex")}
    alex.post("/settings/keys/mine/ANTHROPIC_API_KEY", data={"csrf": csrf_of(alex), "value": CANARIES["alex"]["key"]})
    alex_id = runner.users.by_name("alex").id
    job = make_job(1, title="Plasma Physicist", source="greenhouse", location="Denver, CO")
    for uid, who in ((1, "admin"), (alex_id, "alex")):
        view = store.user(uid)
        view.insert_job(job, "alerted")
        view.update_job(job.key, status="alerted", score=80, score_summary=CANARIES[who]["summary"],
                        top_stories=f'[["{stories[who]}", "{CANARIES[who]["story"]}"]]')
    store.user(alex_id).set_user_state(job.key, "saved")
    vault.install(runner.vault)
    yield runner, app, {"admin": admin, "alex": alex}, stories, job, sent, alex_id
    vault.install(None)
    store.close()


def others(who: str) -> list[str]:
    other = "alex" if who == "admin" else "admin"
    return list(CANARIES[other].values())


def get_paths(app, job_key: str, story_ids: list[str]) -> list[str]:
    paths = []
    for route in app.routes:
        if "GET" not in (getattr(route, "methods", None) or set()):
            continue
        if route.path == "/settings/{section}":
            paths += [f"/settings/{key}" for key, _ in SUBNAV]
        elif "{" not in route.path:
            paths.append(route.path)
    return paths + [f"/jobs/{job_key}", "/?tab=saved", "/?tab=all", "/?tab=step_ups", "/filter?group=all",
                    "/map/embed"] + [f"/stories/{i}" for i in story_ids]


def test_no_page_shows_another_users_data(two):
    runner, app, clients, stories, job, _, _ = two
    for who, client in clients.items():
        mine = CANARIES[who]
        own_pages = client.get("/settings/situation").text + client.get("/settings/profile").text + \
            client.get("/stories").text + client.get(f"/jobs/{job.key}").text
        for value in (mine["salary"], mine["profile"], mine["story"], mine["resume"], mine["summary"]):
            assert value in own_pages, (who, value)                    # each sees their own
        for path in get_paths(app, job.key, list(stories.values())):
            r = client.get(path)
            leaked = [v for v in others(who) if v in r.text or v in str(r.headers)]
            assert not leaked, (who, path, leaked)
        other_story = stories["alex" if who == "admin" else "admin"]
        assert client.get(f"/stories/{other_story}").status_code == 404
    assert "Saved" in clients["alex"].get("/?tab=saved").text
    assert job.key not in clients["admin"].get("/?tab=saved").text     # alex's saved job isn't the admin's


def test_forms_and_key_tests_show_nothing_of_the_other(two):
    runner, app, clients, *_ = two
    for who, client in clients.items():
        token = csrf_of(client)
        for path, data in (("/settings/situation", {"pay.reference_salary": "x"}), ("/filter/save", {"criteria.mode": "x"}),
                           ("/settings/keys/mine/NTFY_TOPIC", {"value": " "}), ("/filter/preview", {}),
                           ("/stories/preview", {"title": "t", "body": "b"})):
            r = client.post(path, data={"csrf": token, **data})
            assert not [v for v in others(who) if v in r.text], (who, path)
        r = client.post("/settings/keys/mine/NTFY_TOPIC/test", headers={"X-CSRF-Token": token})
        assert not [v for v in others(who) if v in r.text], who


def test_pushes_digests_maps_and_home_assistant_are_each_users_own(two):
    runner, app, clients, stories, job, sent, alex_id = two
    alex = runner.people[alex_id]
    assert alex.notifier.topic == CANARIES["alex"]["topic"] and runner.me.notifier.topic == CANARIES["admin"]["topic"]
    assert alex.key_source == "own" and alex.scorer is None or alex.scorer.client.api_key == CANARIES["alex"]["key"]
    digest = run(alex.send_digest(mark=False))
    assert not [v for v in others("alex") if v in digest]
    digest = run(runner.me.send_digest(mark=False))
    assert not [v for v in others("admin") if v in digest]
    run(runner.publish_dashboard())                                     # Home Assistant shows the first user's
    assert sent and not [v for v in others("admin") if v in repr(sent)]
    for who, person in (("admin", runner.me), ("alex", alex)):
        html = run(person.current_map())
        assert not [v for v in others(who) if v in html], who
    # each user's map share link opens their own map only
    for client in clients.values():
        client.post("/settings/account/share-token", data={"csrf": csrf_of(client)})
    guest = TestClient(app)
    alex_token = app.state.web.share_token(alex)
    admin_token = app.state.web.share_token()
    assert alex_token and admin_token and alex_token != admin_token
    alex_map = guest.get(f"/map/embed?token={alex_token}").text
    assert not [v for v in others("alex") if v in alex_map]
    assert run(alex.current_map(shared=True)) == alex_map


def test_the_database_keeps_each_users_keys_encrypted(two):
    runner, app, clients, stories, job, sent, alex_id = two
    runner.store.db.execute("PRAGMA wal_checkpoint(FULL)")
    raw = b"".join(p.read_bytes() for p in runner.data_dir.glob("jobs.db*")).decode("latin-1")
    for who in CANARIES:
        assert CANARIES[who]["topic"] not in raw
    assert CANARIES["alex"]["key"] not in raw
    assert runner.vault.get("ANTHROPIC_API_KEY", alex_id) == CANARIES["alex"]["key"]
    assert runner.vault.get("ANTHROPIC_API_KEY") == ""                    # not the install's
