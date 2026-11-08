"""End-to-end pipeline behaviour with a fake source, in-memory store and dry-run notifier."""
from datetime import timedelta

import pytest

from jobalerts import sources
from jobalerts.config import Board, Config
from jobalerts.http import HttpClient
from jobalerts.models import Job, utcnow
from jobalerts.notify import Notifier
from jobalerts.scheduler import Runner
from jobalerts.scorer import FitScore
from jobalerts.sources.base import Source
from jobalerts.store import Store

from conftest import run


class FakeSource(Source):
    name = "fake"

    def __init__(self):
        self.jobs: list[Job] = []
        self.enriched: list[str] = []

    async def fetch(self, board, http):
        return list(self.jobs)

    async def enrich(self, job, board, http):
        self.enriched.append(job.key)
        job.description = "full description"


class FakeScorer:
    def __init__(self, scores: dict[str, int | None], step_ups: dict[str, int] | None = None):
        self.scores = scores
        self.step_ups = step_ups or {}

    async def score(self, job):
        s = self.scores.get(job.title)
        up = self.step_ups.get(job.title, 0)
        return None if s is None else FitScore(score=s, summary=f"scored {s}", red_flags=[], step_up=up,
                                               step_up_note="leads a larger org" if up else "")


def make_job(n, title="Network Engineer", source="fake", company="Acme", age_days=0.1, location="Remote"):
    return Job(source, "acme" if source == "fake" else "", str(n), company, title, f"https://x/{n}",
               location=location, published_at=utcnow() - timedelta(days=age_days))


@pytest.fixture
def env(monkeypatch):
    fake = FakeSource()
    monkeypatch.setitem(sources.SOURCES, "fake", fake)
    monkeypatch.setattr("jobalerts.scheduler.SOURCES", sources.SOURCES)
    cfg = Config.model_validate({
        "criteria": {"title_include": ["network engineer"], "locations": ["detroit"], "allow_remote": True},
        "polling": {"max_alerts_per_poll": 2},
        "companies": [{"name": "Acme", "ats": "greenhouse", "slug": "acme"}],
    })
    store = Store(":memory:")
    notifier = Notifier(cfg.notify, dry_run=True)
    runner = Runner(cfg, store, HttpClient(), notifier)
    board = Board("fake", "acme", "Acme", 600)
    return fake, runner, board, store, notifier


def poll(runner, board):
    return run(runner.poll_board(board))


def test_first_poll_is_silent_baseline(env):
    fake, runner, board, store, notifier = env
    fake.jobs = [make_job(1), make_job(2)]
    result = poll(runner, board)
    assert result.baseline and result.alerted == 0
    assert notifier.sent == []
    assert store.count_status(("baseline",)) == 2


def test_new_matching_job_alerts_exactly_once(env):
    fake, runner, board, store, notifier = env
    fake.jobs = [make_job(1)]
    poll(runner, board)
    fake.jobs = [make_job(1), make_job(2)]
    assert poll(runner, board).alerted == 1
    assert poll(runner, board).alerted == 0  # seen now; no repeat
    assert len(notifier.sent) == 1
    assert notifier.sent[0]["title"] == "Acme: Network Engineer"
    assert notifier.sent[0]["click"] == "https://x/2"


def test_filtered_stale_and_duplicate_are_not_alerted(env):
    fake, runner, board, store, notifier = env
    fake.jobs = []
    poll(runner, board)
    # Same company+title already seen from another source -> duplicate.
    store.insert_job(make_job(99, source="adzuna", title="Network Engineer II"), "alerted")
    fake.jobs = [
        make_job(1, title="Sales Engineer"),                 # title filter
        make_job(2, location="Austin, TX"),                  # location filter
        make_job(3, age_days=30),                            # stale
        make_job(4, title="Network Engineer II"),            # cross-source duplicate
    ]
    result = poll(runner, board)
    assert result.alerted == 0 and notifier.sent == []
    assert store.count_status(("filtered",)) == 2
    assert store.count_status(("stale",)) == 1
    assert store.count_status(("duplicate",)) == 1


def test_alert_cap_sends_overflow_to_digest(env):
    fake, runner, board, store, notifier = env
    fake.jobs = []
    poll(runner, board)
    fake.jobs = [make_job(n) for n in range(5)]
    result = poll(runner, board)
    assert result.alerted == 2
    assert store.count_status(("digest",)) == 3
    assert notifier.sent[-1]["title"] == "Acme: 3 more matches"


def test_scores_route_to_push_digest_low(env):
    fake, runner, board, store, notifier = env
    runner.scorer = FakeScorer({"Network Engineer A": 80, "Network Engineer B": 60,
                                "Network Engineer C": 20, "Network Engineer D": None})
    fake.jobs = []
    poll(runner, board)
    fake.jobs = [make_job(i, title=f"Network Engineer {c}") for i, c in enumerate("ABCD")]
    poll(runner, board)
    rows = {r["title"]: r for r in store.db.execute("SELECT title, status, score FROM jobs")}
    assert rows["Network Engineer A"]["status"] == "alerted" and rows["Network Engineer A"]["score"] == 80
    assert rows["Network Engineer B"]["status"] == "digest"
    assert rows["Network Engineer C"]["status"] == "low"
    assert rows["Network Engineer D"]["status"] == "alerted"  # no score -> keyword match still pushed
    assert sorted(fake.enriched) == [f"fake:acme:{i}" for i in range(4)]  # descriptions fetched before scoring


def test_clear_step_up_is_pushed_even_with_a_lower_fit(env):
    fake, runner, board, store, notifier = env
    runner.scorer = FakeScorer({"Network Engineer A": 60, "Network Engineer B": 60, "Network Engineer C": 40},
                               step_ups={"Network Engineer A": 82, "Network Engineer B": 70, "Network Engineer C": 90})
    fake.jobs = []
    poll(runner, board)
    fake.jobs = [make_job(i, title=f"Network Engineer {c}") for i, c in enumerate("ABC")]
    poll(runner, board)
    rows = {r["title"]: r for r in store.db.execute("SELECT title, status, step_up, step_up_note FROM jobs")}
    assert rows["Network Engineer A"]["status"] == "alerted" and rows["Network Engineer A"]["step_up"] == 82
    assert rows["Network Engineer B"]["status"] == "digest"   # a real step up, but under the threshold
    assert rows["Network Engineer C"]["status"] == "alerted"  # step-up threshold applies whatever the fit
    message = next(m for m in notifier.sent if m["title"].endswith("Network Engineer A"))
    assert "Step up 82/100: leads a larger org" in message["message"] and message["tags"] == ["rocket"]
    digest = run(runner.send_digest(mark=False))
    assert "step up 70: leads a larger org" in digest
    runner.cfg.scoring.step_up_threshold = 0                      # off: the fit score alone decides
    assert runner.route(60, 95) == "digest"


def test_digest_lists_held_jobs_and_marks_them(env):
    fake, runner, board, store, notifier = env
    fake.jobs = []
    poll(runner, board)
    fake.jobs = [make_job(n) for n in range(4)]
    poll(runner, board)  # 2 pushed, 2 held for the digest

    store.add_suggestion("Newco Inc")
    lookups = []

    async def fake_lookup(name, http):
        lookups.append(name)
        return [discover.Found("greenhouse", "newco", name, 4)]

    import jobalerts.discover as discover
    discover_orig = discover.find_by_name
    discover.find_by_name = fake_lookup
    try:
        message = run(runner.send_digest())
        again = run(runner.send_digest())
    finally:
        discover.find_by_name = discover_orig
    assert "2 jobs worth a look" in message and "https://x/2" in message
    assert "Newco Inc (1 matches) → greenhouse:newco" in message
    assert "Last 24h:" in message
    assert store.count_status(("digest",)) == 0 and store.count_status(("digested",)) == 2
    assert notifier.sent[-2]["title"] == "Job digest: 2 to review"
    assert "No held-back matches" in again and "Newco" not in again  # each item reported once
    assert lookups == ["Newco Inc"]


def test_failing_board_sends_one_health_alert(env, monkeypatch):
    fake, runner, board, store, notifier = env
    runner.cfg.polling.failure_alert_threshold = 3

    async def broken(board, http):
        raise RuntimeError("schema changed")
    fake.fetch = broken

    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) > 6:  # initial stagger + 6 polls, then stop the loop
            raise asyncio.CancelledError

    import asyncio
    monkeypatch.setattr("jobalerts.scheduler.asyncio.sleep", fake_sleep)
    with pytest.raises(asyncio.CancelledError):
        run(runner._board_loop(board))
    alerts = [s for s in notifier.sent if s["title"].startswith("Job board failing")]
    assert len(alerts) == 1 and "schema changed" in alerts[0]["message"]
    assert store.boards()[0]["failures"] == 6
    assert sleeps[2] > sleeps[1]  # exponential backoff between attempts


def test_aggregator_matches_from_unwatched_companies_become_suggestions(env):
    fake, runner, _, store, notifier = env
    agg = Board("fake", "search", "fake 'search'", 600, watched=False)
    fake.jobs = []
    poll(runner, agg)
    fake.jobs = [make_job(1, company="Newco Inc"), make_job(2, company="Acme", title="Network Engineer III")]
    poll(runner, agg)
    names = [r["company"] for r in store.pending_suggestions(10)]
    assert names == ["Newco Inc"]  # Acme is already watched


def test_preview_board_stores_nothing(env):
    fake, runner, board, store, notifier = env
    fake.jobs = [make_job(1), make_job(2, title="Chef"), make_job(3, age_days=20)]
    fetched, matches = run(runner.preview_board(board))
    assert fetched == 3 and [m.job_id for m in matches] == ["1"]
    assert store.count_status(("baseline", "alerted", "filtered")) == 0


def test_best_resume_reaches_alert_db_and_digest(env):
    fake, runner, board, store, notifier = env

    class ResumeScorer:
        async def score(self, job):
            s = 90 if job.title.endswith("A") else 60
            return FitScore(score=s, summary="PIC match", red_flags=[], best_resume="hpc.pdf")

    runner.scorer = ResumeScorer()
    fake.jobs = []
    poll(runner, board)
    fake.jobs = [make_job(1, title="Network Engineer A"), make_job(2, title="Network Engineer B")]
    poll(runner, board)
    assert "Apply with: hpc.pdf" in notifier.sent[-1]["message"]
    assert {r[0] for r in store.db.execute("SELECT best_resume FROM jobs")} == {"hpc.pdf"}
    assert "apply with: hpc.pdf" in run(runner.send_digest(mark=False))


def test_old_database_gains_best_resume_column(tmp_path):
    import sqlite3
    from jobalerts.store import SCHEMA
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.executescript(SCHEMA.replace("    best_resume   TEXT,\n", ""))  # the schema before resume matching
    assert "best_resume" not in {r[1] for r in db.execute("PRAGMA table_info(jobs)")}
    db.close()
    store = Store(path)
    assert "best_resume" in {r["name"] for r in store.db.execute("PRAGMA table_info(jobs)")}
    store.close()


def test_new_match_marks_dashboard_for_refresh(env):
    fake, runner, board, store, notifier = env
    poll(runner, board)  # empty baseline
    assert not runner._dashboard_stale.is_set()
    fake.jobs = [make_job(1, title="Sales Rep")]
    poll(runner, board)
    assert not runner._dashboard_stale.is_set()
    fake.jobs.append(make_job(2))
    poll(runner, board)
    assert runner._dashboard_stale.is_set()


def test_service_start_skips_aggregators_without_keys(env, monkeypatch):
    fake, runner, board, store, notifier = env
    monkeypatch.delenv("USAJOBS_API_KEY", raising=False)
    runner.cfg = runner.cfg.model_copy(update={"aggregators": runner.cfg.aggregators.model_copy(update={
        "usajobs": runner.cfg.aggregators.usajobs.model_copy(update={"enabled": True, "searches": [{"keyword": "x"}]})})})
    started = []

    async def fake_loop(b):
        started.append(b.key)

    async def no_digest():
        return None
    monkeypatch.setattr(runner, "_board_loop", fake_loop)
    monkeypatch.setattr(runner, "_daily_loop", no_digest)
    run(runner.run_forever())
    assert started == ["greenhouse:acme"]
    (msg,) = notifier.sent
    assert "Not running (no API key in .env): usajobs." in msg["message"]


def test_backfill_scores_recent_baseline_matches_once(env):
    fake, runner, board, store, notifier = env
    runner.scorer = FakeScorer({"Network Engineer A": 80, "Network Engineer B": 60, "Network Engineer C": 20})
    fake.jobs = [make_job(i, title=f"Network Engineer {c}") for i, c in enumerate("ABC")]
    fake.jobs += [make_job(7, title="Sales Engineer"), make_job(8, age_days=30)]
    poll(runner, board)  # first poll: everything recorded silently, nothing scored

    results = run(runner.backfill([board]))
    assert {job.title: status for job, status, _ in results} == {
        "Network Engineer A": "digest",  # strong fit, but no --push: held for the digest
        "Network Engineer B": "digest",
        "Network Engineer C": "low",
    }
    assert notifier.sent == []
    rows = {r["title"]: r for r in store.db.execute("SELECT title, status, score FROM jobs")}
    assert rows["Network Engineer A"]["score"] == 80
    assert rows["Sales Engineer"]["status"] == "baseline"   # filtered out: left alone
    assert rows["Network Engineer"]["status"] == "baseline"  # too old: left alone
    assert run(runner.backfill([board])) == []  # each job is done once


def test_backfill_push_sends_strong_fits(env):
    fake, runner, board, store, notifier = env
    runner.scorer = FakeScorer({"Network Engineer A": 80, "Network Engineer B": 60})
    fake.jobs = [make_job(i, title=f"Network Engineer {c}") for i, c in enumerate("AB")]
    poll(runner, board)
    results = run(runner.backfill([board], push=True))
    assert sorted(status for _, status, _ in results) == ["alerted", "digest"]
    assert [m["title"] for m in notifier.sent] == ["Acme: Network Engineer A"]


def test_backfill_leaves_unscored_jobs_for_a_retry(env):
    fake, runner, board, store, notifier = env
    runner.scorer = FakeScorer({})  # every call fails, e.g. a wrong API key
    fake.jobs = [make_job(1)]
    poll(runner, board)
    assert [status for _, status, _ in run(runner.backfill([board], push=True))] == ["unscored"]
    assert notifier.sent == []
    runner.scorer = FakeScorer({"Network Engineer": 80})  # key fixed
    assert [status for _, status, _ in run(runner.backfill([board]))] == ["digest"]
