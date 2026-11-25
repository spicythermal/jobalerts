"""Your own files: config.local.yaml on top of the defaults, moving an old install over, examples."""
import yaml

from jobalerts.config import Config, load_config
from jobalerts.userdata import EXAMPLES, deep_merge, diff_config, migrate_old_config, seed_user_files

DEFAULTS = """
contact_email: "${CONTACT_EMAIL}"
companies_file: ../data/companies.yaml
scoring:
  profile_path: ../data/profile.md
  push_threshold: 75
pay:
  reference_salary: 100000
  reference_location: United States
  current_home: {payment: 0, rate: 0}
notify:
  topic: "${NTFY_TOPIC}"
  timezone: America/New_York
"""


def make_install(tmp_path, local=None):
    (tmp_path / "config").mkdir()
    (tmp_path / "data").mkdir()
    (tmp_path / "config" / "config.yaml").write_text(DEFAULTS)
    if local is not None:
        (tmp_path / "data" / "config.local.yaml").write_text(local)
    return tmp_path / "config" / "config.yaml"


def test_deep_merge_replaces_lists_and_values_and_merges_dicts():
    base = {"a": {"x": 1, "y": [1, 2]}, "b": 2}
    assert deep_merge(base, {"a": {"y": [3]}, "c": 4}) == {"a": {"x": 1, "y": [3]}, "b": 2, "c": 4}
    assert base == {"a": {"x": 1, "y": [1, 2]}, "b": 2}  # inputs untouched


def test_local_config_goes_on_top_and_records_where_values_came_from(tmp_path, monkeypatch):
    monkeypatch.delenv("JOBALERTS_LOCAL_CONFIG")
    path = make_install(tmp_path, "pay:\n  reference_salary: 155000\n  current_home: {payment: 3400}\n")
    cfg = load_config(path)
    assert cfg.pay.reference_salary == 155_000 and cfg.pay.current_home.payment == 3400
    assert cfg.pay.reference_location == "United States" and cfg.scoring.push_threshold == 75
    assert cfg.value_sources == {"pay.reference_salary": "local", "pay.current_home.payment": "local"}


def test_missing_watch_list_falls_back_to_the_example_without_writing(tmp_path, monkeypatch):
    monkeypatch.delenv("JOBALERTS_LOCAL_CONFIG")
    cfg = load_config(make_install(tmp_path))
    example = yaml.safe_load((EXAMPLES / "companies.yaml").read_text())["companies"]
    assert [c.name for c in cfg.companies] == [c["name"] for c in example]
    assert not (tmp_path / "data" / "companies.yaml").exists()


def test_diff_keeps_changed_values_drops_old_file_locations_and_keeps_secret_references():
    old = yaml.safe_load(DEFAULTS.replace("100000", "155000").replace("United States", "Portland, OR")
                         .replace("../data/profile.md", "profile.md").replace("../data/companies.yaml", "companies.yaml"))
    diff = diff_config(old, yaml.safe_load(DEFAULTS))
    assert diff == {"pay": {"reference_salary": 155000, "reference_location": "Portland, OR"}}


def test_old_config_is_migrated_once(tmp_path, monkeypatch):
    monkeypatch.delenv("JOBALERTS_LOCAL_CONFIG")
    monkeypatch.setenv("NTFY_TOPIC", "secret-topic-123")
    path = make_install(tmp_path)
    old = DEFAULTS.replace("timezone: America/New_York", "timezone: America/Los_Angeles") \
                  .replace("push_threshold: 75", "push_threshold: 80")
    (tmp_path / "data" / "config.migrate.yaml").write_text(old)
    cfg = load_config(path)
    assert cfg.notify.timezone == "America/Los_Angeles" and cfg.scoring.push_threshold == 80
    local = (tmp_path / "data" / "config.local.yaml").read_text()
    assert "secret-topic-123" not in local  # ${NTFY_TOPIC} is never expanded into the file
    assert yaml.safe_load(local) == {"scoring": {"push_threshold": 80}, "notify": {"timezone": "America/Los_Angeles"}}
    assert (tmp_path / "data" / "config.migrate.yaml.done").is_file()
    assert not migrate_old_config(path, tmp_path / "data" / "config.local.yaml")  # nothing left to do


def test_seeding_creates_missing_files_from_examples_and_never_overwrites(tmp_path):
    cfg = Config(config_dir=tmp_path / "config")
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "profile.md").write_text("my own profile")
    made = seed_user_files(cfg)
    assert (tmp_path / "data" / "profile.md").read_text() == "my own profile"
    assert (tmp_path / "data" / "companies.yaml").read_text() == (EXAMPLES / "companies.yaml").read_text()
    assert sorted(p.name for p in (tmp_path / "data" / "stories").iterdir()) == \
        sorted(p.name for p in (EXAMPLES / "stories").glob("*.md"))
    assert (tmp_path / "data" / "resumes").is_dir() and len(made) == 2
    assert seed_user_files(cfg) == []
