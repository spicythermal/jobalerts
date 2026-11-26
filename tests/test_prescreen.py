"""AI pre-screen: the Claude calls (fake client) and the pipeline around them (fake pre-screener)."""
from types import SimpleNamespace

import pytest

from jobalerts import sources
from jobalerts.config import Board, Config, Prescreen, Scoring
from jobalerts.http import HttpClient
from jobalerts.notify import Notifier
from jobalerts.prescreen import Prescreener, Verdict, postings_prompt, titles_prompt
from jobalerts.scheduler import Runner
from jobalerts.store import Store

from conftest import run
from test_pipeline import FakeScorer, FakeSource, make_job, poll
from test_scorer import FakeMessages, response


# ---------------------------------------------------------------- Prescreener (Claude calls)

def prescreener(*replies, **scoring):
    store = Store(":memory:")
    messages = FakeMessages(None)
    queue = list(replies)

    async def create(**kwargs):
        messages.calls.append(kwargs)
        return queue.pop(0)
    messages.create = create
    p = Prescreener(Prescreen(), Scoring(**scoring), store, "Plasma physicist; wants R&D leadership.",
                    client=SimpleNamespace(messages=messages))
    return p, messages, store


def test_title_pass_returns_clear_misfits_and_records_usage():
    p, messages, store = prescreener(response({"no": [1, 7]}))
    jobs = [make_job(1, title="Manager, Generator Operations", company="Helion"), make_job(2, title="Payroll Lead")]
    assert run(p.clear_misfits(jobs)) == {1}  # out-of-range numbers are ignored
    call = messages.calls[0]
    assert call["model"] == "claude-haiku-5-5" and call["output_config"]["effort"] == "low"
    assert call["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "Plasma physicist" in call["system"][0]["text"]
    assert "0. Manager, Generator Operations | Helion | Remote" in call["messages"][0]["content"]
    assert "clearly NOT worth" in call["messages"][0]["content"]
    assert store.ai_calls_today() == 1


def test_posting_pass_counts_left_out_postings_as_yes():
    p, messages, _ = prescreener(response({"results": [{"id": 0, "verdict": "no", "reason": "payroll"}]}))
    jobs = [make_job(1, title="Payroll Lead"), make_job(2, title="Lead, Beam Operations")]
    jobs[1].description = "x" * 5000
    verdicts = run(p.judge(jobs))
    assert verdicts[0].verdict == "no" and verdicts[0].reason == "payroll"
    assert verdicts[1].verdict == "yes"
    prompt = messages.calls[0]["messages"][0]["content"]
    assert "x" * 3000 in prompt and "x" * 3001 not in prompt  # excerpt_chars
    assert "(no description available)" in prompt


def test_failed_or_capped_calls_return_none():
    p, _, _ = prescreener(response(None))  # unparseable answer
    assert run(p.clear_misfits([make_job(1)])) is None
    p, messages, _ = prescreener(daily_max_calls=0)
    assert run(p.judge([make_job(1)])) is None and messages.calls == []


def test_prompts_number_jobs_from_zero():
    jobs = [make_job(1, title="A"), make_job(2, title="B")]
    assert "0. A |" in titles_prompt(jobs) and "1. B |" in titles_prompt(jobs)
    assert '<posting id="1">' in postings_prompt(jobs, 100)


# ---------------------------------------------------------------- pipeline

class FakePrescreener:
    def __init__(self, title_no=(), posting_no=(), fail=False):
        self.title_no, self.posting_no, self.fail = set(title_no), set(posting_no), fail
        self.seen_titles, self.seen_postings = [], []

    async def clear_misfits(self, jobs):
        self.seen_titles += [j.title for j in jobs]
        return None if self.fail else {i for i, j in enumerate(jobs) if j.title in self.title_no}

    async def judge(self, jobs):
        self.seen_postings += [(j.title, j.description) for j in jobs]
        return {i: Verdict(id=i, verdict="no" if j.title in self.posting_no else "yes", reason="because")
                for i, j in enumerate(jobs)}


@pytest.fixture
def env(monkeypatch):
    fake = FakeSource()
    monkeypatch.setitem(sources.SOURCES, "fake", fake)
    monkeypatch.setattr("jobalerts.scheduler.SOURCES", sources.SOURCES)
    cfg = Config.model_validate({
        "criteria": {"title_include": ["network engineer"], "title_exclude": ["sales"]},
        "polling": {"max_alerts_per_poll": 5},
    })
    store = Store(":memory:")
    notifier = Notifier(cfg.notify, dry_run=True)
    scorer = FakeScorer({"Network Engineer": 80, "Manager, Generator Operations": 84, "Beam Lead": 55})
    pre = FakePrescreener(title_no={"Payroll Clerk"}, posting_no={"Facilities Planner"})
    runner = Runner(cfg, store, HttpClient(), notifier, scorer, prescreener=pre)
    board = Board("fake", "acme", "Acme", 600)
    fake.jobs = []
    poll(runner, board)  # baseline
    return fake, runner, board, store, notifier, pre


def statuses(store):
    return {r["title"]: (r["status"], r["reason"]) for r in store.db.execute("SELECT title, status, reason FROM owner_jobs")}


def test_rejected_titles_wait_for_the_prescreen_but_hard_excludes_do_not(env):
    fake, runner, board, store, notifier, pre = env
    fake.jobs = [make_job(1, title="Network Engineer"), make_job(2, title="Manager, Generator Operations"),
                 make_job(3, title="Sales Director")]
    fake.jobs[1].description = "Run the fusion generator."  # list APIs that include the description
    result = poll(runner, board)
    s = statuses(store)
    assert s["Network Engineer"][0] == "alerted"           # keyword match: scored and pushed right away
    assert s["Manager, Generator Operations"][0] == "prescreen"
    assert s["Sales Director"] == ("filtered", "title excludes 'sales'")
    assert result.alerted == 1
    stored = store.db.execute("SELECT description FROM owner_jobs WHERE title='Manager, Generator Operations'").fetchone()
    assert stored[0] == "Run the fusion generator."       # kept while it waits


def test_prescreen_run_filters_misfits_and_scores_the_rest(env):
    fake, runner, board, store, notifier, pre = env
    fake.jobs = [make_job(1, title="Manager, Generator Operations"), make_job(2, title="Payroll Clerk"),
                 make_job(3, title="Facilities Planner"), make_job(4, title="Beam Lead")]
    fake.jobs[0].description = "Run the fusion generator."
    poll(runner, board)
    sent_before = len(notifier.sent)
    counts = run(runner.run_prescreen([board]))
    s = statuses(store)
    assert s["Payroll Clerk"] == ("filtered", "pre-screen: title")
    assert s["Facilities Planner"] == ("filtered", "pre-screen: because")
    assert s["Manager, Generator Operations"][0] == "alerted"   # 84: pushed
    assert s["Beam Lead"][0] == "digest"                         # 55: evening digest
    assert counts == {"title_no": 1, "posting_no": 1, "alert": 1, "digest": 1}
    assert len(notifier.sent) == sent_before + 1
    # the stored description reached pass 2; the others were fetched from the board
    assert ("Manager, Generator Operations", "Run the fusion generator.") in pre.seen_postings
    assert ("Beam Lead", "full description") in pre.seen_postings
    assert store.db.execute("SELECT COUNT(*) FROM owner_jobs WHERE description IS NOT NULL").fetchone()[0] == 0
    assert run(runner.run_prescreen([board])) == {}  # queue empty: nothing left to do


def test_jobs_keep_waiting_when_claude_is_unavailable(env):
    fake, runner, board, store, notifier, pre = env
    pre.fail = True
    fake.jobs = [make_job(1, title="Beam Lead")]
    poll(runner, board)
    assert run(runner.run_prescreen([board])) == {"waiting": 1}
    assert statuses(store)["Beam Lead"][0] == "prescreen"
    pre.fail = False
    assert run(runner.run_prescreen([board])) == {"digest": 1}


def test_waiting_jobs_go_stale(env):
    fake, runner, board, store, notifier, pre = env
    fake.jobs = [make_job(1, title="Beam Lead", age_days=6.9)]
    poll(runner, board)
    store.db.execute("UPDATE jobs SET published_at='2026-01-01T00:00:00+00:00'")
    assert run(runner.run_prescreen([board])) == {"stale": 1}
    assert pre.seen_titles == []


def test_without_prescreener_rejected_titles_are_filtered(env):
    fake, runner, board, store, notifier, pre = env
    runner.prescreener = None
    fake.jobs = [make_job(1, title="Beam Lead")]
    poll(runner, board)
    assert statuses(store)["Beam Lead"][0] == "filtered"


def test_prescreen_needs_scoring():
    cfg = Config()
    runner = Runner(cfg, Store(":memory:"), HttpClient(), Notifier(cfg.notify, dry_run=True),
                    scorer=None, prescreener=FakePrescreener())
    assert runner.prescreener is None


def test_turning_the_prescreen_off_releases_the_queue(tmp_path):
    store = Store(tmp_path / "j.db")
    store.hold_for_prescreen(make_job(1, title="Beam Lead"), "title not in include list")
    assert len(store.prescreen_queue()) == 1
    assert store.drop_prescreen_queue() == 1
    row = store.db.execute("SELECT status, description FROM owner_jobs").fetchone()
    assert (row["status"], row["description"]) == ("filtered", None)


def test_old_database_gains_description_column(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE jobs (key TEXT PRIMARY KEY, source TEXT NOT NULL, board TEXT NOT NULL, "
               "job_id TEXT NOT NULL, company TEXT, title TEXT, url TEXT, location TEXT, remote INTEGER, "
               "salary TEXT, published_at TEXT, first_seen_at TEXT NOT NULL, fingerprint TEXT, "
               "status TEXT NOT NULL, reason TEXT, score INTEGER, score_summary TEXT, red_flags TEXT, notified_at TEXT)")
    db.commit()
    db.close()
    store = Store(path)
    store.hold_for_prescreen(make_job(1), "x")
    assert store.prescreen_queue()[0]["description"] is None
