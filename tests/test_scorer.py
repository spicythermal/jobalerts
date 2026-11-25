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
    row = store.ai_usage()[0]
    assert (row["input_tokens"], row["output_tokens"], row["cache_read_tokens"]) == (100, 40, 900)


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


def test_resumes_go_in_the_prompt_and_best_resume_is_checked():
    from jobalerts.resumes import Resume
    resumes = [Resume("rnd.pdf", "Led a 7-person FIB team"), Resume("hpc.pdf", "PIC simulation on HPC")]
    for picked, expected in (("hpc.pdf", "hpc.pdf"), ("made-up.pdf", "")):
        messages = FakeMessages(response({"score": 80, "summary": "PIC match", "red_flags": [],
                                          "best_resume": picked}))
        s = Scorer(Scoring(), Store(":memory:"), "", client=SimpleNamespace(messages=messages), resumes=resumes)
        result = run(s.score(JOB))
        system = messages.calls[0]["system"][0]["text"]
        assert '<resume file="rnd.pdf">\nLed a 7-person FIB team\n</resume>' in system
        assert '<resume file="hpc.pdf">' in system and "(no profile provided)" in system
        assert result.best_resume == expected
    assert "best_resume" in messages.calls[0]["output_config"]["format"]["schema"]["required"]


def test_make_scorer_loads_resume_folder(monkeypatch, tmp_path):
    from jobalerts.cli import make_scorer
    from jobalerts.config import Config
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    (tmp_path / "resumes").mkdir()
    (tmp_path / "resumes" / "mine.md").write_text("Ion optics group leader", encoding="utf-8")
    cfg = Config(scoring={"enabled": True, "profile_path": "missing.md", "resume_dir": "resumes",
                          "stories_dir": "no-stories"}, config_dir=tmp_path)
    s = make_scorer(cfg, Store(":memory:"))
    assert s is not None and s.resume_names == ["mine.md"] and "Ion optics group leader" in s.system
    cfg.scoring.resume_dir = "nothing-here"
    assert make_scorer(cfg, Store(":memory:")) is None  # no profile and no resumes: keyword-only


def test_prompt_truncates_huge_descriptions():
    big = Job("x", "", "1", "A", "T", "u", description="y" * 50_000)
    assert "[description truncated]" in job_prompt(big)


def test_reads_posted_pay_but_leaves_the_floor_to_the_program():
    s, messages, _ = scorer(response({"score": 80, "summary": "ok", "red_flags": [], "best_resume": "",
                                      "pay_min": 170000, "pay_max": 150000, "pay_currency": "usd",
                                      "pay_period": "year"}))
    result = run(s.score(JOB))
    pay = result.pay()
    assert (pay.low, pay.high, pay.currency, pay.period) == (150000, 170000, "USD", "year")
    call = messages.calls[0]
    assert "pay_max" in call["output_config"]["format"]["schema"]["required"]
    assert "cost of living" in call["system"][0]["text"]
    assert "pay below the candidate's floor" not in call["system"][0]["text"]


def test_no_pay_stated_means_no_pay():
    s, _, _ = scorer(response({"score": 80, "summary": "ok", "red_flags": [], "pay_min": 0, "pay_max": 0}))
    assert run(s.score(JOB)).pay() is None


def test_step_up_is_asked_for_separately_and_clamped():
    s, messages, _ = scorer(response({"score": 62, "summary": "x", "red_flags": [], "step_up": 130,
                                      "step_up_note": "Director over three group leaders"}))
    result = run(s.score(JOB))
    assert result.score == 62 and result.step_up == 100 and result.step_up_note == "Director over three group leaders"
    schema = messages.calls[0]["output_config"]["format"]["schema"]
    assert {"step_up", "step_up_note"} <= set(schema["required"])
    assert "a level up" in messages.calls[0]["system"][0]["text"]
    s, _, _ = scorer(response({"score": 50, "summary": "x", "red_flags": []}))  # an answer without it: 0
    assert run(s.score(JOB)).step_up == 0


def test_cap_logs_once_a_day_and_basis_follows_the_prompt(caplog):
    import logging
    s, messages, store = scorer(response({"score": 50, "summary": "ok", "red_flags": []}), daily_max_calls=1)
    assert s.calls_left() == 1 and run(s.score(JOB)).basis == s.basis and s.capped()
    with caplog.at_level(logging.WARNING, logger="jobalerts.scorer"):
        assert run(s.score(JOB)) is None and run(s.score(JOB)) is None
    assert sum("daily cap" in r.message for r in caplog.records) == 1 and len(messages.calls) == 1
    other, _, _ = scorer(response(None))
    other.system += " changed stories"
    assert Scorer(Scoring(), store, "different profile", client=other.client).basis != s.basis
