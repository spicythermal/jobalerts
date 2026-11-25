"""Secret vault: encrypted at rest, write-only from the UI, .env first, redacted from logs."""
import logging

import pytest

from jobalerts import vault
from jobalerts.config import Notify
from jobalerts.notify import Notifier
from jobalerts.sources import SOURCES
from jobalerts.store import Store

KEY = b"v" * 32
CANARY = "sk-canary-0123456789"


@pytest.fixture
def v(tmp_path, monkeypatch):
    for name in vault.SECRETS:
        monkeypatch.delenv(name, raising=False)
    store = Store(tmp_path / "jobs.db")
    yield vault.Vault(store, KEY)
    vault.install(None)
    store.close()


def test_secrets_are_encrypted_at_rest_and_round_trip(v, tmp_path):
    v.set("ANTHROPIC_API_KEY", f"  {CANARY}  ")
    assert v.get("ANTHROPIC_API_KEY") == CANARY
    v.store.db.execute("PRAGMA wal_checkpoint(FULL)")
    raw = b"".join(p.read_bytes() for p in tmp_path.glob("jobs.db*"))
    assert CANARY.encode() not in raw
    with pytest.raises(KeyError):
        v.set("SOMETHING_ELSE", "x")
    with pytest.raises(ValueError):
        v.set("NTFY_TOKEN", "   ")
    v.clear("ANTHROPIC_API_KEY")
    assert v.get("ANTHROPIC_API_KEY") == ""


def test_status_tells_where_a_secret_comes_from_but_never_its_value(v, monkeypatch):
    v.set("ANTHROPIC_API_KEY", CANARY)
    v.set("USAJOBS_EMAIL", "me@example.com")
    monkeypatch.setenv("NTFY_TOPIC", "topic-from-env-123")
    status = {s.name: s for s in v.status()}
    assert status["ANTHROPIC_API_KEY"].is_set and status["ANTHROPIC_API_KEY"].source == "ui"
    assert status["NTFY_TOPIC"].source == "env" and status["ADZUNA_APP_KEY"].source == ""
    assert CANARY not in repr(v.status()) and "topic-from-env-123" not in repr(v.status())
    assert status["USAJOBS_EMAIL"].shown_value == "me@example.com"   # not a secret; may be shown


def test_environment_wins_over_the_stored_value(v, monkeypatch):
    v.set("RAPIDAPI_KEY", "stored-value-1")
    vault.install(v)
    assert vault.secret("RAPIDAPI_KEY") == "stored-value-1"
    monkeypatch.setenv("RAPIDAPI_KEY", "env-value-22")
    assert vault.secret("RAPIDAPI_KEY") == "env-value-22" and v.get("RAPIDAPI_KEY") == "env-value-22"


def test_a_different_key_cant_read_stored_secrets(v, caplog):
    v.set("THEIRSTACK_API_KEY", CANARY)
    other = vault.Vault(v.store, b"w" * 32)
    assert other.get("THEIRSTACK_API_KEY") == "" and "can't be decrypted" in caplog.text


def test_runtime_users_see_stored_keys(v):
    v.set("NTFY_TOPIC", "my-topic-from-ui")
    v.set("ADZUNA_APP_ID", "id-123456")
    notifier = Notifier(Notify())
    assert notifier.dry_run and SOURCES["adzuna"].missing_keys() == ["ADZUNA_APP_ID", "ADZUNA_APP_KEY"]
    vault.install(v)
    assert not notifier.dry_run and notifier.build("t", "m")["topic"] == "my-topic-from-ui"
    assert SOURCES["adzuna"].missing_keys() == ["ADZUNA_APP_KEY"]


def test_logs_never_show_secrets(v, caplog):
    v.set("ADZUNA_APP_KEY", CANARY)
    vault.install(v)
    redactor = vault.install_log_redaction()
    caplog.handler.addFilter(redactor)
    log = logging.getLogger("jobalerts.test")
    log.warning("Client error for url https://api.adzuna.com/v1/api/jobs/us/search/1?app_id=abc12345&app_key=%s", CANARY)
    try:
        raise RuntimeError(f"boom with {CANARY}")
    except RuntimeError:
        log.exception("failed")
    assert CANARY not in caplog.text and "abc12345" not in caplog.text
    assert "app_key=[redacted]" in caplog.text and "boom with [redacted]" in caplog.text


def test_key_from_environment(monkeypatch, tmp_path):
    from jobalerts.config import Config
    monkeypatch.setenv("JOBALERTS_SECRET_KEY", "dG9vIHNob3J0")
    with pytest.raises(ValueError):
        vault.secret_key(Config(database=str(tmp_path / "x.db")))
    monkeypatch.delenv("JOBALERTS_SECRET_KEY")
    key = vault.secret_key(Config(database=str(tmp_path / "x.db")))
    assert len(key) == 32 and (tmp_path / "secret.key").stat().st_mode & 0o777 == 0o600
