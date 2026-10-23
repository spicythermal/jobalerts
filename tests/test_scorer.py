import json
from types import SimpleNamespace

from jobalerts.config import Scoring
from jobalerts.models import Job
from jobalerts.scorer import Scorer, job_prompt
from jobalerts.store import Store

from conftest import run


def response(payload, stop_reason="end_turn"):
    return SimpleNamespace(
        stop_reason=stop_reason,
        content=[SimpleNamespace(type="thinking", thinking=""),
                 SimpleNamespace(type="text", text=json.dumps(payload) if payload is not None else "")],
        usage=SimpleNamespace(input_tokens=100, output_tokens=40, cache_read_input_tokens=900,
                              cache_creation_input_tokens=0),
    )


class FakeMessages:
    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.reply


def scorer(reply, **cfg):
    messages = FakeMessages(reply)
    store = Store(":memory:")
    s = Scorer(Scoring(**cfg), store, "Wants network roles. Floor $140K.",
               client=SimpleNamespace(messages=messages))
    return s, messages, store


JOB = Job("greenhouse", "acme", "1", "Acme", "Network Engineer", "https://x", location="Remote",
          description="BGP all day", salary="$150K")


def test_scores_and_records_usage():
    s, messages, store = scorer(response({"score": 87, "summary": "Strong BGP match", "red_flags": []}))
    result = run(s.score(JOB))
    assert result.score == 87 and result.summary == "Strong BGP match"
    call = messages.calls[0]
    assert call["model"] == "claude-haiku-5-5"
    assert call["output_config"]["effort"] == "low"
    assert call["output_config"]["format"]["type"] == "json_schema"
    assert call["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "Floor $140K" in call["system"][0]["text"]
    assert "BGP all day" in call["messages"][0]["content"]
    assert store.ai_calls_today() == 1
    assert store.ai_usage()[0]["input_tokens"] == 1000


def test_daily_cap_stops_calls():
    s, messages, store = scorer(response({"score": 50, "summary": "ok", "red_flags": []}), daily_max_calls=2)
    for _ in range(3):
        run(s.score(JOB))
    assert len(messages.calls) == 2


def test_refusal_and_bad_json_return_none():
    s, _, _ = scorer(response({"score": 1, "summary": "x", "red_flags": []}, stop_reason="refusal"))
    assert run(s.score(JOB)) is None
    s, _, _ = scorer(response(None))
    assert run(s.score(JOB)) is None


def test_score_clamped():
    s, _, _ = scorer(response({"score": 140, "summary": "x", "red_flags": ["a"]}))
    assert run(s.score(JOB)).score == 100


def test_shipped_profile_template_loads(monkeypatch):
    from jobalerts.cli import make_scorer
    from jobalerts.config import load_config
    from conftest import REPO
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")  # client construction only; no request is made
    cfg = load_config(REPO / "config" / "config.yaml")
    cfg.scoring.enabled = True
    s = make_scorer(cfg, Store(":memory:"))
    assert s is not None and "<candidate_profile>" in s.system and "Deal-breakers" in s.system


def test_prompt_truncates_huge_descriptions():
    big = Job("x", "", "1", "A", "T", "u", description="y" * 50_000)
    assert "[description truncated]" in job_prompt(big)
