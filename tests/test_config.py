import pytest

from jobalerts.config import Company, Config, load_config, parse_workday_url

from jobalerts.sources import without_missing_keys

from conftest import REPO


def test_shipped_config_loads(monkeypatch):
    monkeypatch.setenv("NTFY_TOPIC", "my-topic")
    cfg = load_config(REPO / "config" / "config.yaml")
    assert cfg.notify.topic == "my-topic"
    assert cfg.companies, "companies.yaml should ship with a starter watch list"
    boards = cfg.boards()
    assert all(b.interval in cfg.polling.tiers.values() for b in boards if b.watched)
    # Everything ships enabled; aggregators without API keys in .env are skipped, not failed.
    assert cfg.scoring.enabled and cfg.homeassistant.enabled
    assert {b.source for b in boards if not b.watched} == {"usajobs", "adzuna", "jsearch", "theirstack"}
    for key in ("USAJOBS_API_KEY", "USAJOBS_EMAIL", "ADZUNA_APP_ID", "ADZUNA_APP_KEY", "RAPIDAPI_KEY",
                "THEIRSTACK_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    kept, skipped = without_missing_keys(boards)
    assert len(kept) == sum(c.enabled for c in cfg.companies)
    assert skipped == {"usajobs": ["USAJOBS_API_KEY", "USAJOBS_EMAIL"], "adzuna": ["ADZUNA_APP_ID", "ADZUNA_APP_KEY"],
                       "jsearch": ["RAPIDAPI_KEY"], "theirstack": ["THEIRSTACK_API_KEY"]}
    assert [b.key for b in boards if b.source == "theirstack"] == ["theirstack:physics"]
    monkeypatch.setenv("RAPIDAPI_KEY", "k")
    assert {b.source for b in without_missing_keys(boards)[0] if not b.watched} == {"jsearch"}


def test_parse_workday_url():
    assert parse_workday_url("https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/job/x") == (
        "nvidia.wd5.myworkdayjobs.com", "nvidia", "NVIDIAExternalCareerSite")
    assert parse_workday_url("https://acme.wd1.myworkdayjobs.com/Careers")[2] == "Careers"


def test_company_validation():
    with pytest.raises(ValueError):
        Company(name="X", ats="greenhouse")
    with pytest.raises(ValueError):
        Company(name="X", ats="workday", url="https://example.com")
    Company(name="X", ats="workday", url="https://x.wd1.myworkdayjobs.com/Ext")
    with pytest.raises(ValueError):
        Company(name="X", ats="ultipro", url="https://recruiting.ultipro.com/ACME/JobBoard/not-a-guid")
    Company(name="X", ats="ultipro", url="https://recruiting.ultipro.com/ACM1/JobBoard/49386e5d-f909-4648-8335-415ce97e3f15")


def test_boards_include_enabled_aggregators():
    cfg = Config.model_validate({
        "companies": [{"name": "Acme", "ats": "greenhouse", "slug": "acme", "tier": "priority"},
                      {"name": "Off", "ats": "lever", "slug": "off", "enabled": False},
                      {"name": "Big", "ats": "workday", "url": "https://big.wd5.myworkdayjobs.com/en-US/Ext"}],
        "aggregators": {"usajobs": {"enabled": True, "searches": [{"keyword": "IT Specialist"}]}},
    })
    keys = {b.key: b for b in cfg.boards()}
    assert set(keys) == {"greenhouse:acme", "workday:big/Ext", "usajobs:it-specialist"}
    assert keys["greenhouse:acme"].interval == 180
    assert keys["usajobs:it-specialist"].watched is False
    assert keys["workday:big/Ext"].params["host"] == "big.wd5.myworkdayjobs.com"


def test_adzuna_search_can_pick_its_country():
    cfg = Config.model_validate({"aggregators": {"adzuna": {"enabled": True, "country": "us", "searches": [
        {"what": "physicist", "where": "Oregon"}, {"what": "physicist", "country": "de"}]}}})
    boards = {b.key: b.params["country"] for b in cfg.boards()}
    assert boards == {"adzuna:physicist": "us", "adzuna:physicist-de": "de"}


def test_unknown_tier_rejected():
    cfg = Config.model_validate({"companies": [{"name": "A", "ats": "lever", "slug": "a", "tier": "turbo"}]})
    with pytest.raises(ValueError, match="unknown tier"):
        cfg.boards()
