"""The Filter page and the AI-only mode: the funnel, previews, saving, "score it anyway", and the
cascade (hard excludes, then the AI's first look in batches, then the full score)."""
import re
from html import unescape
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from jobalerts import sources, vault
from jobalerts.config import Board, Config, Criteria, Prescreen, Scoring
from jobalerts.filters import group_of
from jobalerts.http import HttpClient
from jobalerts.notify import Notifier
from jobalerts.prescreen import Prescreener
from jobalerts.scheduler import AI_ONLY_WAITING, Runner
from jobalerts.settings import UiSettings
from jobalerts.store import Store
from jobalerts.web.app import create_app

from conftest import run
from test_pipeline import FakeScorer, FakeSource, make_job, poll
from test_prescreen import FakePrescreener, statuses
from test_scorer import FakeMessages, response

PASSWORD = "filter page test password"


# ---------------------------------------------------------------- the pre-screen's instructions

def system_of(prescreen: Prescreen, criteria: Criteria) -> tuple[str, str]:
    messages = FakeMessages(None)

    async def create(**kwargs):
        messages.calls.append(kwargs)
        return response({"no": []})
    messages.create = create
    p = Prescreener(prescreen, Scoring(), Store(":memory:"), "Optics engineer.", client=SimpleNamespace(messages=messages),
                    criteria=criteria)
    run(p.clear_misfits([make_job(1)]))
    return messages.calls[0]["system"][0]["text"], messages.calls[0]["model"]


def test_first_look_prompt_uses_your_words_and_its_own_model():
    text, model = system_of(Prescreen(model="claude-sonnet-5-5", guidance="Turn down sales roles."),
                            Criteria(mode="ai", title_field=["*optic*", "laser"]))
    assert model == "claude-sonnet-5-5"
    assert "new postings from the candidate's job boards" in text and "keyword filter" not in text
    assert "their field, in their own words: optic, laser" in text
    assert "<candidate_guidance>\nTurn down sales roles.\n</candidate_guidance>" in text
    assert "Optics engineer." in text
    assert "physics" not in text.lower()          # nobody's field is built in any more
    text, model = system_of(Prescreen(), Criteria())
    assert model == "claude-haiku-5-5" and "keyword filter has already rejected" in text
    assert "candidate_guidance" not in text and "in their own words" not in text


def test_funnel_groups():
    assert group_of("filtered", "title not in include list") == "no_word"
    assert group_of("filtered", "title excludes 'sales'") == "hard_exclude"
    assert group_of("filtered", "pre-screen: title") == "ai_title"
    assert group_of("filtered", "pre-screen: payroll role") == "ai_posting"
    assert group_of("prescreen", AI_ONLY_WAITING) == "waiting"
    assert group_of("alerted", "") == "pushed" and group_of("digested", "") == "digest"
    assert group_of("baseline", "") is None and group_of("closed", "taken down") == "other"


# ---------------------------------------------------------------- AI-only mode in the pipeline

@pytest.fixture
def ai_env(monkeypatch):
    fake = FakeSource()
    monkeypatch.setitem(sources.SOURCES, "fake", fake)
    monkeypatch.setattr("jobalerts.scheduler.SOURCES", sources.SOURCES)
    cfg = Config.model_validate({
        "criteria": {"mode": "ai", "title_include": ["network engineer"], "title_exclude": ["sales"],
                     "locations": ["Denver"]},
        "polling": {"max_alerts_per_poll": 5}, "prescreen": {"enabled": True, "ai_delay": 0},
    })
    store = Store(":memory:")
    notifier = Notifier(cfg.notify, dry_run=True)
    scorer = FakeScorer({"Optical Designer": 81, "Network Engineer": 60, "Plasma Lead": 90})
    pre = FakePrescreener(title_no={"Payroll Clerk"}, posting_no={"Facilities Planner"})
    runner = Runner(cfg, store, HttpClient(), notifier, scorer, prescreener=pre)
    board = Board("fake", "acme", "Acme", 600)
    monkeypatch.setattr(runner, "active_boards", lambda: [board])
    fake.jobs = []
    poll(runner, board)  # baseline
    return fake, runner, board, store, notifier, pre


def test_ai_only_sends_every_job_but_hard_excludes_through_the_cascade(ai_env):
    fake, runner, board, store, notifier, pre = ai_env
    fake.jobs = [make_job(1, title="Optical Designer"), make_job(2, title="Sales Director"),
                 make_job(3, title="Payroll Clerk"), make_job(4, title="Facilities Planner"),
                 make_job(5, title="Network Engineer")]
    poll(runner, board)
    s = statuses(store)
    assert s["Sales Director"] == ("filtered", "title excludes 'sales'")     # hard exclude: no AI call
    # no word lists, no location list (Remote isn't Denver): all four wait for the AI's first look
    assert all(s[t] == ("prescreen", AI_ONLY_WAITING)
               for t in ("Optical Designer", "Payroll Clerk", "Facilities Planner", "Network Engineer"))
    assert runner._prescreen_wake.is_set()
    counts = run(runner.run_prescreen([board]))
    s = statuses(store)
    assert s["Payroll Clerk"] == ("filtered", "pre-screen: title")
    assert s["Facilities Planner"][0] == "filtered" and s["Facilities Planner"][1].startswith("pre-screen:")
    assert s["Optical Designer"][0] == "alerted" and s["Network Engineer"][0] == "digest"
    assert counts == {"title_no": 1, "posting_no": 1, "alert": 1, "digest": 1}
    assert pre.seen_titles == ["Optical Designer", "Payroll Clerk", "Facilities Planner", "Network Engineer"]
    assert len(notifier.sent) == 1


def test_ai_only_without_the_ai_falls_back_to_the_word_lists(ai_env):
    fake, runner, board, store, notifier, pre = ai_env
    runner.prescreener = None
    fake.jobs = [make_job(1, title="Optical Designer"), make_job(2, title="Network Engineer", location="Denver, CO")]
    poll(runner, board)
    s = statuses(store)
    assert s["Optical Designer"] == ("filtered", "title not in include list")
    assert s["Network Engineer"][0] == "digest"


def test_score_anyway_scores_a_turned_down_job_without_a_push(ai_env):
    fake, runner, board, store, notifier, pre = ai_env
    runner.cfg.criteria.mode = "keywords"
    runner.prescreener = None
    fake.jobs = [make_job(1, title="Plasma Lead", location="Denver")]
    poll(runner, board)
    assert statuses(store)["Plasma Lead"] == ("filtered", "title not in include list")
    key = store.db.execute("SELECT key FROM owner_jobs WHERE title='Plasma Lead'").fetchone()[0]
    assert run(runner.score_anyway(key)) == "digest"          # 90 would push; on request it goes to the digest
    row = store.get_job(key)
    assert row["status"] == "digest" and row["score"] == 90 and notifier.sent == []
    assert run(runner.score_anyway("fake:nope:1")) == "not found"
    runner.scorer.left = 0
    assert run(runner.score_anyway(key)) == "today's AI calls are used up"


# ---------------------------------------------------------------- the page

@pytest.fixture
def page_env(tmp_path, monkeypatch):
    for name in vault.SECRETS:
        monkeypatch.delenv(name, raising=False)
    cfg = Config(config_dir=tmp_path / "config", database=str(tmp_path / "jobs.db"), pay={"enabled": False},
                 criteria={"title_include": ["engineer"], "title_exclude": ["sales"]},
                 prescreen={"enabled": True})
    store = Store(cfg.database)
    for n, title, status, reason in [
            (1, "Optical Engineer", "digested", "title matched 'engineer'"),
            (2, "Sales Engineer", "filtered", "title excludes 'sales'"),
            (3, "Laser Physicist", "filtered", "title not in include list"),
            (4, "Payroll Clerk", "filtered", "pre-screen: title"),
            (5, "Old Engineer", "stale", "published 40 days ago"),
            (6, "First-poll Job", "baseline", "")]:
        store.insert_job(make_job(n, title=title), status, reason)
    runner = Runner(cfg, store, HttpClient(), Notifier(cfg.notify, dry_run=True), FakeScorer({}),
                    prescreener=FakePrescreener())
    runner.vault = vault.Vault(store, b"k" * 32)
    runner.ui_settings = UiSettings(store, runner.vault)
    app = create_app(runner, b"f" * 32)
    with TestClient(app, follow_redirects=False) as client:
        token = re.search(r'name="csrf" value="([^"]+)"', client.get("/setup").text).group(1)
        client.post("/setup", data={"csrf": token, "code": app.state.web.setup_code, "password": PASSWORD, "again": PASSWORD})
        token = re.search(r'name="csrf" value="([^"]+)"', client.get("/filter").text).group(1)
        yield runner, client, token
    store.close()


def form(**changes) -> dict:
    data = {"criteria.mode": "keywords", "prescreen.enabled": "1", "prescreen.model": "", "scoring.model": "claude-haiku-5-5",
            "prescreen.guidance": "", "criteria.title_field": "", "criteria.title_include": "engineer",
            "criteria.title_exclude": "sales", "criteria.title_exclude_soft": "", "criteria.seniority_exclude": "",
            "criteria.locations": "", "criteria.allow_remote": "1", "criteria.max_age_days": "7",
            "criteria.description_exclude": ""}
    data.update(changes)
    return {k: v for k, v in data.items() if v is not None}


def test_page_shows_the_funnel_and_what_was_turned_down(page_env):
    runner, client, token = page_env
    html = unescape(client.get("/filter").text)
    assert "<strong>5</strong> new jobs" in html and "<strong>4</strong> turned down" in html
    assert "1 open at a board's first poll" in html
    for label in ("Title has an exclude or seniority word", "Title has no field or role word",
                  "AI first look: title clearly not a fit", "Posted too long ago"):
        assert label in html
    for title in ("Sales Engineer", "Laser Physicist", "Payroll Clerk", "Old Engineer"):
        assert title in html
    assert "Score it</button>" in html and "calls a day" in html
    only = unescape(client.get("/filter?group=no_word").text)
    jobs_part = only.split('id="jobs"')[1].split("</section>")[0]
    assert "Laser Physicist" in jobs_part and "Payroll Clerk" not in jobs_part
    assert client.get("/settings/search").headers["location"] == "/filter"
    assert '/filter"' in client.get("/").text            # in the nav


def test_preview_and_try_use_the_edited_lists_without_saving(page_env):
    runner, client, token = page_env
    h = {"X-CSRF-Token": token}
    html = unescape(client.post("/filter/preview", data=form(**{"criteria.title_field": "*physicist"}), headers=h).text)
    assert "1 would now get through" in html and "Laser Physicist" in html and "score it now" in html
    html = unescape(client.post("/filter/preview", data=form(**{"criteria.title_exclude": "sales\noptical"}), headers=h).text)
    assert "1 would now be turned down" in html and "Optical Engineer" in html
    html = unescape(client.post("/filter/preview", data=form(**{"criteria.mode": "ai"}), headers=h).text)
    assert "would now get through" in html and "Laser Physicist" in html and "Sales Engineer" not in html.split("get through")[1]
    html = unescape(client.post("/filter/try", data=form(try_title="Senior Laser Physicist"), headers=h).text)
    assert "turned down" in html and "AI second look still sees it" in html
    html = unescape(client.post("/filter/try", data=form(try_title="Laser Physicist",
                                                         **{"criteria.title_field": "*physicist"}), headers=h).text)
    assert "gets through" in html
    assert runner.cfg.criteria.title_field == []         # nothing was saved


def test_saving_switches_to_ai_only_live(page_env):
    runner, client, token = page_env
    r = client.post("/filter/save", data={"csrf": token, **form(**{"criteria.mode": "ai", "prescreen.enabled": None})})
    assert r.status_code == 200 and "AI-only mode needs the AI first look on" in unescape(r.text)
    assert runner.cfg.criteria.mode == "keywords"
    r = client.post("/filter/save", data={"csrf": token, **form(**{"criteria.mode": "ai", "prescreen.model": "claude-haiku-5-5",
                                                                     "scoring.model": "claude-sonnet-5-5",
                                                                     "prescreen.guidance": "Keep lab roles."})})
    assert r.status_code == 303 and r.headers["location"] == "/filter?saved=1"
    cfg = runner.cfg
    assert (cfg.criteria.mode, cfg.prescreen.model, cfg.scoring.model, cfg.prescreen.guidance) == \
        ("ai", "claude-haiku-5-5", "claude-sonnet-5-5", "Keep lab roles.")
    assert runner.ui_settings.values()["criteria.mode"] == "ai"
    assert "AI only:" in unescape(client.get("/filter").text)
    r = client.post("/filter/save", data={"csrf": token, **form(**{"scoring.model": "gpt-4"})})
    assert "Pick one of the models." in unescape(r.text)
    client.post("/filter/reset", data={"csrf": token})
    assert runner.cfg.criteria.mode == "keywords" and runner.ui_settings.values() == {}


def test_score_anyway_button(page_env, monkeypatch):
    runner, client, token = page_env
    called = []

    async def fake(key):
        called.append(key)
        runner.store.update_job(key, status="digest", score=77)
        return "digest"
    monkeypatch.setattr(runner, "score_anyway", fake)
    key = runner.store.db.execute("SELECT key FROM owner_jobs WHERE title='Laser Physicist'").fetchone()[0]
    html = client.post(f"/filter/score/{key}", headers={"X-CSRF-Token": token}).text
    assert called == [key] and "score <strong>77</strong>" in html and "in the digest" in html
    assert client.post(f"/filter/score/{key}").status_code == 403           # no CSRF token
    assert client.post("/filter/score/fake:acme:999", headers={"X-CSRF-Token": token}).status_code == 404
