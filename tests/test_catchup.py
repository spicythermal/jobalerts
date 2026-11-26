"""Run anywhere: catching up after the computer was off or asleep, the missed digest, look-back over
the gap for aggregator searches, state that survives a restart, and one service per database."""
import asyncio
import re
from datetime import datetime, timedelta, timezone

import pytest

from jobalerts import cli, runstate
from jobalerts.config import Board, Config
from jobalerts.sources import SOURCES
from jobalerts.sources.base import lookback_days

from conftest import json_routes, mock_http, run
from test_pipeline import FakeScorer, env, make_job  # noqa: F401 (env is a fixture)

THREE_DAYS_AGO = runstate.now() - timedelta(days=3)


def setup_catchup(env):
    fake, runner, board, store, notifier = env
    runner.active_boards = lambda: [board]
    fake.jobs = [make_job(0)]
    run(runner.poll_board(board))                       # baseline before the computer went off
    titles = {"Network Engineer 95": 95, "Network Engineer 90": 90, "Network Engineer 85": 85,
              "Network Engineer 80": 80, "Network Engineer 60": 60, "Network Engineer 30": 30}
    runner.scorer = FakeScorer(titles)
    fake.jobs += [make_job(i + 1, title=t) for i, t in enumerate(titles)]
    return fake, runner, board, store, notifier


def test_catch_up_sends_one_summary_and_the_best_three(env):
    fake, runner, board, store, notifier = setup_catchup(env)
    report = run(runner.catch_up(THREE_DAYS_AGO))
    titles = [m["title"] for m in notifier.sent]
    assert titles[0].startswith("Catch-up: 6 new matches since") and "(4 strong)" in titles[0]
    assert titles[1:] == ["Acme: Network Engineer 95", "Acme: Network Engineer 90", "Acme: Network Engineer 85"]
    status = {r["title"]: r["status"] for r in store.db.execute("SELECT title, status FROM owner_jobs")}
    assert status["Network Engineer 95"] == "alerted" and status["Network Engineer 80"] == "digest"
    assert status["Network Engineer 60"] == "digest" and status["Network Engineer 30"] == "low"
    assert report["gap"] == "3.0 days" and runner.catchup_report()["summary"] == report["summary"]
    assert "taken down" in report["note"]
    # nothing new afterwards: a second catch-up stays quiet
    notifier.sent.clear()
    run(runner.catch_up(THREE_DAYS_AGO))
    assert notifier.sent == []


def test_catch_up_records_failing_boards(env):
    fake, runner, board, store, notifier = env
    runner.active_boards = lambda: [board]

    async def broken(board, http):
        raise RuntimeError("board down")
    fake.fetch = broken
    report = run(runner.catch_up(THREE_DAYS_AGO))
    assert "1 boards failed" in report["summary"]
    assert store.boards()[0]["failures"] == 1


def test_start_catches_up_only_after_a_gap_then_sends_a_missed_digest(env, monkeypatch):
    fake, runner, board, store, notifier = env
    calls = []

    async def fake_catch_up(since):
        calls.append(("catch_up", since))

    async def fake_digest(mark=True):
        calls.append(("digest",))

    async def no_boards():
        calls.append(("boards",))
    monkeypatch.setattr(runner, "catch_up", fake_catch_up)
    monkeypatch.setattr(runner, "send_digest", fake_digest)
    monkeypatch.setattr(runner, "_supervise_boards", no_boards)
    monkeypatch.setattr(runner, "digest_missed", lambda: True)
    run(runner._start_boards(THREE_DAYS_AGO))
    assert calls == [("catch_up", THREE_DAYS_AGO), ("digest",), ("boards",)]   # in that order
    calls.clear()
    monkeypatch.setattr(runner, "digest_missed", lambda: False)
    run(runner._start_boards(runstate.now() - timedelta(minutes=10)))           # a quick restart
    run(runner._start_boards(None))                                              # the very first start
    assert calls == [("boards",), ("boards",)]


def test_missed_digest(env):
    fake, runner, board, store, notifier = env
    tz = runner.cfg.notify.timezone
    from zoneinfo import ZoneInfo
    evening = datetime(2026, 11, 20, 20, 0, tzinfo=ZoneInfo(tz))      # after the 18:00 digest
    morning = datetime(2026, 11, 20, 9, 0, tzinfo=ZoneInfo(tz))
    assert not runner.digest_missed(evening)                           # never sent one: nothing to catch up
    store.set_meta(runstate.LAST_DIGEST, "2026-11-19")
    assert runner.digest_missed(evening) and not runner.digest_missed(morning)
    store.set_meta(runstate.LAST_DIGEST, "2026-11-17")
    assert runner.digest_missed(morning)
    run(runner.send_digest())
    assert store.get_meta(runstate.LAST_DIGEST) == datetime.now(ZoneInfo(tz)).date().isoformat()


def test_waking_up_triggers_a_catch_up(env, monkeypatch):
    fake, runner, board, store, notifier = env
    runner.cfg.run.heartbeat = 0
    t0 = datetime(2026, 11, 20, 8, 0, tzinfo=timezone.utc)
    times = iter([t0, t0 + timedelta(minutes=1), t0 + timedelta(hours=9)])
    monkeypatch.setattr(runstate, "now", lambda: next(times))
    caught = []

    async def fake_catch_up(since):
        caught.append(since)
        raise asyncio.CancelledError    # end the loop for the test
    monkeypatch.setattr(runner, "catch_up", fake_catch_up)
    with pytest.raises(asyncio.CancelledError):
        run(runner._heartbeat_loop())
    assert caught == [t0 + timedelta(minutes=1)]
    assert runstate.last_heartbeat(store) == t0 + timedelta(minutes=1)


def test_searches_look_back_over_the_gap(monkeypatch):
    assert lookback_days(None, 2, 30) == 2
    assert lookback_days(runstate.now() - timedelta(days=5, hours=3), 2, 30) == 7
    assert lookback_days(runstate.now() - timedelta(days=90), 2, 30) == 30
    seen = []

    def handler(request):
        seen.append(dict(request.url.params))
        return json_routes({"adzuna": {"results": []}})(request)
    http = mock_http(handler)
    board = Board("adzuna", "physicist", "adzuna 'physicist'", 3600, {"what": "physicist", "country": "us"}, watched=False)
    adzuna = SOURCES["adzuna"]
    monkeypatch.setenv("ADZUNA_APP_ID", "id-test-1")
    monkeypatch.setenv("ADZUNA_APP_KEY", "key-test-1")
    adzuna.resume(board, runstate.now() - timedelta(days=4))
    run(adzuna.fetch(board, http))
    run(adzuna.fetch(board, http))
    assert [p["max_days_old"] for p in seen] == ["6", "2"]   # back over the gap once, then the usual window
    theirstack, ts_board = SOURCES["theirstack"], Board("theirstack", "physics", "theirstack", 86400, {})
    theirstack.resume(ts_board, THREE_DAYS_AGO)
    assert theirstack._since[ts_board.key] == THREE_DAYS_AGO
    jsearch, js_board = SOURCES["jsearch"], Board("jsearch", "q", "jsearch", 14400, {})
    jsearch.resume(js_board, runstate.now() - timedelta(days=5))
    assert jsearch._window[js_board.key] == "week"


def test_alert_and_cap_notices_survive_a_restart(env):
    fake, runner, board, store, notifier = env
    runner._failure_alerted.add("fake:acme")
    runner._save_failure_alerted()
    store.set_meta(runstate.CAP_NOTICED, "2026-11-20")
    from jobalerts.scheduler import Runner
    again = Runner(runner.cfg, store, runner.http, notifier)
    assert again._failure_alerted == {"fake:acme"} and again._cap_noticed == "2026-11-20"


def test_one_service_per_database(tmp_path, capsys):
    lock = runstate.InstanceLock(tmp_path / "jobalerts.lock")
    assert lock.acquire()
    other = runstate.InstanceLock(tmp_path / "jobalerts.lock")
    assert not other.acquire() and other.holder().isdigit()
    cfg = Config(database=str(tmp_path / "jobs.db"))
    assert run(cli.cmd_run(cfg, None)) == 1 and "already running" in capsys.readouterr().out
    lock.release()
    assert other.acquire()
    other.release()


def test_jobs_page_shows_the_report(env):
    from fastapi.testclient import TestClient
    from jobalerts.web.app import create_app
    fake, runner, board, store, notifier = setup_catchup(env)
    run(runner.catch_up(THREE_DAYS_AGO))
    app = create_app(runner, b"c" * 32)
    client = TestClient(app, follow_redirects=False)
    token = re.search(r'name="csrf" value="([^"]+)"', client.get("/setup").text).group(1)
    client.post("/setup", data={"csrf": token, "code": app.state.web.setup_code, "password": "catch up test pw",
                                "again": "catch up test pw"})
    html = client.get("/").text
    assert "Since you were away" in html and "3.0 days" in html and "taken down" in html
