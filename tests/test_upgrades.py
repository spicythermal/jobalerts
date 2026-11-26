"""Updates keep what's yours: settings a new version renamed, removed or no longer accepts are dropped one
by one (never all of them), the database is backed up before an upgrade changes it, and nothing personal
reaches anyone who isn't logged in."""
import re
import sqlite3

import pytest
import yaml
from pydantic import ValidationError
from fastapi.testclient import TestClient

from jobalerts import settings as st, userdata, vault
from jobalerts.config import load_config
from jobalerts.http import HttpClient
from jobalerts.notify import Notifier
from jobalerts.scheduler import Runner
from jobalerts.store import KEEP_BACKUPS, SCHEMA_VERSION, Store
from jobalerts.web.app import create_app

from conftest import REPO, with_reference_home
from test_pipeline import make_job

DEFAULTS = (REPO / "config" / "config.yaml").read_text()


def install(tmp_path, local: dict) -> "Path":  # noqa: F821
    (tmp_path / "config").mkdir()
    (tmp_path / "data").mkdir()
    (tmp_path / "config" / "config.yaml").write_text(DEFAULTS)
    for name in ("col_data.json", "taxes.yaml"):
        (tmp_path / "config" / name).write_bytes((REPO / "config" / name).read_bytes())
    (tmp_path / "config" / "tax_data").symlink_to(REPO / "config" / "tax_data")
    (tmp_path / "data" / "config.local.yaml").write_text(yaml.safe_dump(local))
    return tmp_path / "config" / "config.yaml"


def test_a_bad_or_unknown_local_setting_is_dropped_alone(tmp_path, monkeypatch):
    monkeypatch.setenv("JOBALERTS_LOCAL_CONFIG", str(tmp_path / "data" / "config.local.yaml"))
    path = install(tmp_path, {"pay": {"reference_salary": 155000, "min_index": "very high"},
                              "scoring": {"push_threshold": 80, "fancy_new_thing": True},
                              "notify": {"timezone": "America/Denver"}})
    cfg = load_config(path)    # an older version would refuse to start here
    assert cfg.pay.reference_salary == 155000 and cfg.scoring.push_threshold == 80
    assert cfg.notify.timezone == "America/Denver" and cfg.pay.min_index == 1.05   # the default stands in
    text = " | ".join(cfg.config_problems)
    assert "pay.min_index in config.local.yaml isn't valid" in text
    assert "scoring.fancy_new_thing in config.local.yaml isn't used by this version" in text
    assert "pay.min_index" not in cfg.value_sources and cfg.value_sources["pay.reference_salary"] == "local"


def test_renamed_settings_carry_over(tmp_path, monkeypatch):
    monkeypatch.setenv("JOBALERTS_LOCAL_CONFIG", str(tmp_path / "data" / "config.local.yaml"))
    monkeypatch.setattr(userdata, "RENAMED", {"pay.old_floor": "pay.min_index"})
    monkeypatch.setattr(st, "RENAMED", userdata.RENAMED)
    cfg = load_config(install(tmp_path, {"pay": {"old_floor": 1.2}}))
    assert cfg.pay.min_index == 1.2 and "renamed in this version: pay.old_floor → pay.min_index" in cfg.config_problems
    store = Store(tmp_path / "data" / "jobs.db")
    v = vault.Vault(store, b"u" * 32)
    store.db.executescript(st.SCHEMA)
    store.db.execute("INSERT INTO settings VALUES ('pay.old_floor', '1.3', 0, 'x')")
    ui = st.UiSettings(store, v)
    assert ui.values()["pay.min_index"] == 1.3


def test_saved_ui_settings_survive_one_bad_value(tmp_path):
    store = Store(tmp_path / "jobs.db")
    v = vault.Vault(store, b"u" * 32)
    ui = st.UiSettings(store, v)
    ui.save({"scoring.push_threshold": 82, "pay.reference_salary": 123_456})
    store.db.execute("UPDATE settings SET value='\"lots\"' WHERE key='scoring.push_threshold'")   # a bad value
    store.db.execute("INSERT INTO settings VALUES ('scoring.gone_in_this_version', '1', 0, 'x')")
    base = with_reference_home(load_config(REPO / "config" / "config.yaml"))
    cfg = st.apply(base, ui.overlay())
    assert cfg.pay.reference_salary == 123_456 and cfg.scoring.push_threshold == 75   # the good one stays
    assert any("scoring.push_threshold" in p for p in cfg.config_problems)
    assert ui.unknown() == ["scoring.gone_in_this_version"]                          # kept, reported
    with pytest.raises(ValidationError):
        st.apply(base, ui.overlay(), strict=True)                                     # a form save says why


def old_database(path):
    """A database as an earlier version left it: no schema version, a pay note with housing figures."""
    store = Store(path)
    job = make_job(1)
    store.insert_job(job, "alerted")
    store.db.execute("UPDATE jobs SET pay_note=? WHERE key=?", (
        "pay index 1.00 rent · 1.00 buy: $155K/yr in Portland-Vancouver-Hillsboro, OR-WA metro (RPP 105.4; staying "
        "in your home at 4.5%: $3,400 payment - $750 principal + $450 upkeep + $374 utilities = $3.5K/mo); staying "
        "in your home at 4.5%: $3,400 payment - $750 principal + $450 upkeep + $374 utilities = $3.5K/mo", job.key))
    store.db.execute("PRAGMA user_version = 0")
    store.close()
    return job.key


def test_the_database_is_backed_up_and_cleaned_once_on_upgrade(tmp_path):
    key = old_database(tmp_path / "jobs.db")
    store = Store(tmp_path / "jobs.db")
    backups = list((tmp_path / "backups").glob("jobs-*.db"))
    assert len(backups) == 1 and store.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    note = store.get_job(key)["pay_note"]
    assert "$3,400" not in note and "$750" not in note and note.count("staying in your home") == 2
    old = sqlite3.connect(backups[0])
    assert "$3,400" in old.execute("SELECT pay_note FROM jobs").fetchone()[0]   # the backup is the old data
    old.close()
    store.close()
    Store(tmp_path / "jobs.db").close()
    assert len(list((tmp_path / "backups").glob("jobs-*.db"))) == 1               # nothing more to back up
    store = Store(tmp_path / "jobs.db")
    for _ in range(KEEP_BACKUPS + 2):
        store.backup("manual")
    assert len(list((tmp_path / "backups").glob("jobs-*.db"))) == KEEP_BACKUPS


def test_a_newer_database_is_left_alone(tmp_path, caplog):
    store = Store(tmp_path / "jobs.db")
    store.db.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    store.close()
    Store(tmp_path / "jobs.db").close()
    assert "newer jobalerts" in caplog.text and not (tmp_path / "backups").exists()


PERSONAL = {"pay.reference_salary": "123,457", "pay.reference_location": "Boulder, CO", "pay.current_home.payment": "4,321",
            "pay.current_home.principal": "987", "pay.current_home.rate": "3.25", "contact_email": "me@private.example"}
CANARIES = ("123457", "123,457", "Boulder", "4,321", "4321", "$987", "3.25%", "me@private.example")


def test_nothing_personal_without_a_login(tmp_path, monkeypatch):
    for name in vault.SECRETS:
        monkeypatch.delenv(name, raising=False)
    cfg = with_reference_home(load_config(REPO / "config" / "config.yaml"))
    cfg.config_dir = tmp_path / "config"
    for name in ("col_data", "tax_rules", "tax_data"):
        setattr(cfg.pay, name, str(REPO / "config" / getattr(cfg.pay, name)))
    runner = Runner(cfg, Store(tmp_path / "jobs.db"), HttpClient(), Notifier(cfg.notify, dry_run=True))
    app = create_app(runner, b"p" * 32)
    owner = TestClient(app, follow_redirects=False)
    token = re.search(r'name="csrf" value="([^"]+)"', owner.get("/setup").text).group(1)
    owner.post("/setup", data={"csrf": token, "code": app.state.web.setup_code, "password": "a long password!",
                               "again": "a long password!"})
    html = owner.get("/settings/situation").text
    form = {"csrf": re.search(r'name="csrf" value="([^"]+)"', html).group(1)}
    form.update(dict(re.findall(r'<input id="f-\d+" name="([^"]+)" value="([^"]*)"', html)))
    for name, options in re.findall(r'<select id="f-\d+" name="([^"]+)">(.*?)</select>', html, re.S):
        form[name] = re.search(r'<option value="([^"]+)" selected>', options).group(1)
    form.update(PERSONAL)
    assert owner.post("/settings/situation", data=form).status_code == 303
    job = make_job(1, location="Boulder, CO")
    job.salary = "$150K-170K/yr"
    runner.store.insert_job(job, "alerted")
    assert "123,457" in owner.get("/settings/situation").text                  # the owner sees it
    guest = TestClient(app, follow_redirects=False)
    paths = [r.path for r in app.routes if "GET" in (getattr(r, "methods", None) or set()) and "{" not in r.path]
    paths += [f"/jobs/{job.key}", "/settings/situation", "/settings/keys", "/stories/x", "/map/embed?token="]
    for path in paths:
        r = guest.get(path)
        leaked = [c for c in CANARIES if c in r.text or c in str(r.headers)]
        assert not leaked, (path, leaked)
    owner.post("/settings/account/share-token", data={"csrf": form["csrf"]})
    shared = guest.get(f"/map/embed?token={app.state.web.share_token()}").text
    # By design (and said so on the Account page): the home metro, the salary the break-evens are against,
    # and the top jobs' places. Never the housing figures or the contact email.
    for c in ("4,321", "4321", "$987", "3.25%", "me@private.example"):
        assert c not in shared, c
