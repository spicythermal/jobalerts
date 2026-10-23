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
    def __init__(self, scores: dict[str, int | None]):
        self.scores = scores

    async def score(self, job):
        s = self.scores.get(job.title)
        return None if s is None else FitScore(score=s, summary=f"scored {s}", red_flags=[])


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
