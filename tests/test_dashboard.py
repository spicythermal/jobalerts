"""Top matches ranking and the Home Assistant sensor."""
import json
from datetime import timedelta

import httpx

from jobalerts.cli import main
from jobalerts.config import Config, HomeAssistant
from jobalerts.dashboard import HomeAssistantPublisher, top_matches
from jobalerts.filters import JobFilter
from jobalerts.models import Job, utcnow
from jobalerts.store import Store

from conftest import run

CRITERIA = {"title_include": ["physicist"], "locations": ["portland"], "allow_remote": True}


def job(n, title="Plasma Physicist", location="Portland, OR", age_days=1.0):
    return Job("greenhouse", "acme", str(n), "Acme", title, f"https://x/{n}", location=location,
               published_at=utcnow() - timedelta(days=age_days))


def test_top_matches_ranks_by_score_then_newest_and_includes_baseline():
    store = Store(":memory:")
    store.insert_job(job(1, age_days=0.5), "baseline")                    # already open, matches
    store.insert_job(job(2, title="Accountant"), "baseline")               # already open, no match
    store.insert_job(job(3, location="Austin, TX"), "baseline")            # wrong location
    store.insert_job(job(4, age_days=10), "baseline")                      # too old for the window
    store.insert_job(job(5, age_days=2), "processing")
    store.update_job("greenhouse:acme:5", status="digest", score=60, best_resume="cv.pdf")
    store.insert_job(job(6, age_days=3), "processing")
    store.update_job("greenhouse:acme:6", status="alerted", score=90)
    store.insert_job(job(7), "filtered")
    store.insert_job(job(8, age_days=0.2), "alert")                        # keyword match, no score

    total, jobs = top_matches(store, JobFilter(Config.model_validate({"criteria": CRITERIA}).criteria), 7, 3)
    assert total == 4
    assert [j["url"] for j in jobs] == ["https://x/6", "https://x/5", "https://x/8"]
    assert jobs[0]["pushed"] and jobs[1]["best_resume"] == "cv.pdf"
    assert jobs[2]["age"] == "5h ago"


def test_home_assistant_publish_sets_sensor_state():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(201, json={})

    cfg = HomeAssistant(enabled=True, url="http://ha:8123/", token="secret")
    jobs = [{"company": "Acme", "title": "Plasma Physicist", "url": "https://x/1"}]

    async def go():
        ha = HomeAssistantPublisher(cfg, transport=httpx.MockTransport(handler))
        await ha.publish(4, jobs)
        await ha.aclose()
    run(go())

    (req,) = seen
    assert str(req.url) == "http://ha:8123/api/states/sensor.job_alerts"
    assert req.headers["Authorization"] == "Bearer secret"
    body = json.loads(req.content)
    assert body["state"] == 4
    assert body["attributes"]["top"] == "Acme: Plasma Physicist"
    assert body["attributes"]["jobs"] == jobs


def test_top_command_prints_matches(tmp_path, capsys, monkeypatch):
    db = tmp_path / "jobs.db"
    store = Store(db)
    store.insert_job(job(1), "baseline")
    store.close()
    (tmp_path / "config.yaml").write_text(json.dumps({"criteria": CRITERIA}), encoding="utf-8")
    monkeypatch.setenv("JOBALERTS_DB", str(db))
    assert main(["-c", str(tmp_path / "config.yaml"), "top"]) == 0
    out = capsys.readouterr().out
    assert "1 matches posted in the last 7 days" in out and "Acme: Plasma Physicist" in out
    assert main(["-c", str(tmp_path / "config.yaml"), "top", "--push"]) == 1  # Home Assistant not set up
