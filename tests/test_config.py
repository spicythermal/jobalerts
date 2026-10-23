import pytest

from jobalerts.config import Company, Config, load_config, parse_workday_url

from conftest import REPO


def test_shipped_config_loads(monkeypatch):
    monkeypatch.setenv("NTFY_TOPIC", "my-topic")
    cfg = load_config(REPO / "config" / "config.yaml")
    assert cfg.notify.topic == "my-topic"
    assert cfg.companies, "companies.yaml should ship with a starter watch list"
    boards = cfg.boards()
    assert len(boards) == sum(c.enabled for c in cfg.companies)  # aggregators are off by default
    assert all(b.interval in cfg.polling.tiers.values() for b in boards)


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


def test_unknown_tier_rejected():
    cfg = Config.model_validate({"companies": [{"name": "A", "ats": "lever", "slug": "a", "tier": "turbo"}]})
    with pytest.raises(ValueError, match="unknown tier"):
        cfg.boards()
