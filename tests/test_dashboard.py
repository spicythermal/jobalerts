"""Top matches ranking and the Home Assistant sensor."""
import json
from datetime import timedelta

from jobalerts.cli import main
from jobalerts.config import Config, HomeAssistant
from jobalerts.dashboard import MqttPublisher, top_matches
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


def test_mqtt_publish_sends_retained_discovery_attributes_and_state():
    sent = []
    cfg = HomeAssistant(enabled=True, mqtt_host="ha.lan", interval=300)
    jobs = [{"company": "Acme", "title": "Plasma Physicist", "url": "https://x/1"}]
    run(MqttPublisher(cfg, send=lambda c, msgs: sent.extend(msgs)).publish(4, jobs))

    topics = dict(sent)
    discovery = json.loads(topics["homeassistant/sensor/jobalerts/top/config"])
    assert discovery["name"] is None and discovery["device"]["name"] == "Job alerts"  # -> sensor.job_alerts
    assert discovery["state_topic"] == "jobalerts/top/state"
    assert discovery["json_attributes_topic"] == "jobalerts/top/attributes"
    assert discovery["expire_after"] == 900
    assert topics["jobalerts/top/state"] == "4"
    attributes = json.loads(topics["jobalerts/top/attributes"])
    assert attributes["top"] == "Acme: Plasma Physicist" and attributes["jobs"] == jobs
    assert [t for t, _ in sent][-1] == "jobalerts/top/state"  # attributes land before the state changes


def test_paho_messages_are_retained_with_login(monkeypatch):
    calls = {}
    from paho.mqtt import publish
    monkeypatch.setattr(publish, "multiple", lambda msgs, **kw: calls.update(msgs=msgs, **kw))
    cfg = HomeAssistant(enabled=True, mqtt_host="ha.lan", mqtt_username="jobalerts", mqtt_password="pw")
    run(MqttPublisher(cfg).publish(0, []))
    assert calls["hostname"] == "ha.lan" and calls["port"] == 1883
    assert calls["auth"] == {"username": "jobalerts", "password": "pw"} and calls["tls"] is None
    assert all(m["retain"] and m["qos"] == 1 for m in calls["msgs"])


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


def test_ai_cost_month_today_and_week():
    from jobalerts.config import Scoring
    from jobalerts.dashboard import ai_cost
    store = Store(":memory:")
    assert ai_cost(store, Scoring(enabled=False)) is None  # no scoring -> no sensor
    today = utcnow().date()
    last_month = (today.replace(day=1) - timedelta(days=1)).isoformat()
    store.db.execute("INSERT INTO ai_usage(day, calls, input_tokens, output_tokens) VALUES(?,?,?,?)",
                     (last_month, 100, 10_000_000, 0))              # $1.00, not this month
    for _ in range(10):  # today: 10 calls, 1K uncached + 9K cached input, 400 output each
        store.record_ai_call(1_000, 400, cache_read_tokens=9_000)
    cost = ai_cost(store, Scoring(enabled=True))
    # 10 x (1000 x $0.10 + 400 x $0.50 + 9000 x $0.01) / 1M = $0.0039
    assert cost["month"] == cost["today"] == 0.0039
    assert cost["calls_month"] == 10 and cost["model"] == "claude-haiku-5-5"
    assert cost["last_7_days"] in (0.0039, 1.0039)  # last month's day falls inside the week early in a month


def test_mqtt_publishes_cost_sensor_only_when_given():
    sent = []
    cfg = HomeAssistant(enabled=True, mqtt_host="ha.lan", interval=300)
    publisher = MqttPublisher(cfg, send=lambda c, msgs: sent.extend(msgs))
    run(publisher.publish(0, []))
    assert not any("cost" in t for t, _ in sent)

    cost = {"month": 0.12345, "today": 0.01, "last_7_days": 0.05, "calls_month": 250}
    run(publisher.publish(0, [], cost))
    topics = dict(sent)
    discovery = json.loads(topics["homeassistant/sensor/jobalerts/cost/config"])
    assert discovery["name"] == "AI cost this month" and discovery["device_class"] == "monetary"
    assert discovery["unit_of_measurement"] == "USD" and discovery["unique_id"] == "jobalerts_ai_cost"
    assert discovery["device"] == json.loads(topics["homeassistant/sensor/jobalerts/top/config"])["device"]
    assert topics["jobalerts/cost/state"] == "0.1235"
    assert json.loads(topics["jobalerts/cost/attributes"])["calls_month"] == 250


def test_old_ai_usage_table_gets_cache_columns(tmp_path):
    import sqlite3
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE ai_usage (day TEXT PRIMARY KEY, calls INTEGER NOT NULL DEFAULT 0, "
                "input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0)")
    con.execute("INSERT INTO ai_usage VALUES ('2026-10-24', 3, 9000, 300)")
    con.commit()
    con.close()
    store = Store(db)
    store.record_ai_call(100, 10, cache_read_tokens=50)
    rows = store.ai_usage_since("2026-10-01")
    assert [(r["calls"], r["cache_read_tokens"]) for r in rows] == [(3, 0), (1, 50)]


def test_top_step_ups_rank_by_the_step_up_score():
    store = Store(":memory:")
    for n, (score, step_up) in enumerate([(90, 20), (55, 85), (70, 60)], 1):
        store.insert_job(job(n), "processing")
        store.update_job(f"greenhouse:acme:{n}", status="digest", score=score, step_up=step_up,
                         step_up_note=f"note {n}")
    store.insert_job(job(4), "alert")                                      # no AI score: not a step-up candidate
    flt = JobFilter(Config.model_validate({"criteria": CRITERIA}).criteria)
    assert [j["url"] for j in top_matches(store, flt, 7, 10)[1]][:3] == ["https://x/1", "https://x/3", "https://x/2"]
    total, ups = top_matches(store, flt, 7, 2, by="step_up")
    assert total == 4 and [(j["url"], j["step_up_note"]) for j in ups] == [("https://x/2", "note 2"), ("https://x/3", "note 3")]


def test_sensor_attributes_carry_the_step_ups():
    pub = MqttPublisher(HomeAssistant(mqtt_host="h"))
    attrs = json.loads(pub.messages(1, [{"company": "A", "title": "T"}], None, [{"title": "Director"}])[1][1])
    assert attrs["step_ups"] == [{"title": "Director"}]
