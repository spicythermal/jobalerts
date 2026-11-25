"""API keys and other secrets: from the environment (.env) or, when set on the Settings page, stored
encrypted in the database.

- Encryption: Fernet (AES-128-CBC + HMAC-SHA256) under a key derived from `secret.key` in the data
  folder (0600, created on first use), or from the JOBALERTS_SECRET_KEY environment variable. A copy
  or backup of the database alone doesn't reveal the secrets; someone with the whole data folder (or
  the machine) can read them, as jobalerts itself must.
- One way only: the web UI can set, replace or clear a secret and see whether it is set and where it
  comes from (`status()`), never its value. Only the running service reads values, via `secret()`.
- Precedence: a value in the real environment or .env wins over a stored one (the page says so).
- Logs: `RedactingFilter` blanks every known secret value and key-like URL parameters.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os
import re
import secrets as _random
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

log = logging.getLogger(__name__)

# name -> (label, shown?) – "shown" values aren't secret (an email address) and may be displayed.
SECRETS: dict[str, tuple[str, bool]] = {
    "ANTHROPIC_API_KEY": ("Anthropic (Claude) API key, for AI scoring", False),
    "NTFY_TOPIC": ("ntfy topic (anyone who knows it can read your alerts)", False),
    "NTFY_TOKEN": ("ntfy access token (optional)", False),
    "MQTT_PASSWORD": ("MQTT password, for Home Assistant", False),
    "USAJOBS_API_KEY": ("USAJobs API key", False),
    "USAJOBS_EMAIL": ("USAJobs account email", True),
    "ADZUNA_APP_ID": ("Adzuna app id", False),
    "ADZUNA_APP_KEY": ("Adzuna app key", False),
    "RAPIDAPI_KEY": ("RapidAPI key (JSearch)", False),
    "THEIRSTACK_API_KEY": ("TheirStack API key", False),
}
SCHEMA = """
CREATE TABLE IF NOT EXISTS secrets (
    name       TEXT PRIMARY KEY,
    ciphertext TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""
MIN_REDACT = 6  # shorter values would blank ordinary words in log lines


@dataclass
class SecretStatus:
    name: str
    label: str
    is_set: bool
    source: str          # "env" (.env or the environment), "ui" (stored), or ""
    updated_at: str      # when it was stored via the UI
    shown_value: str     # only for values that aren't secret (an email address); "" otherwise


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def load_or_create_key(path: Path) -> bytes:
    """32 random bytes kept in `path` (created 0600 on first use): the vault and the web sessions."""
    if path.is_file():
        key = _unb64(path.read_text(encoding="ascii").strip())
        if len(key) == 32:
            return key
        raise ValueError(f"{path} doesn't hold a 32-byte key; move it away to create a new one "
                         "(stored secrets and web sessions are then lost)")
    path.parent.mkdir(parents=True, exist_ok=True)
    key = _random.token_bytes(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as f:
        f.write(_b64(key) + "\n")
    return key


def secret_key(cfg) -> bytes:
    """JOBALERTS_SECRET_KEY (base64 of 32 bytes) if set, else data/secret.key next to the database."""
    if os.environ.get("JOBALERTS_SECRET_KEY"):
        key = _unb64(os.environ["JOBALERTS_SECRET_KEY"].strip())
        if len(key) != 32:
            raise ValueError("JOBALERTS_SECRET_KEY must be 32 bytes, base64 (e.g. from: openssl rand -base64 32)")
        return key
    return load_or_create_key(Path(cfg.database).resolve().parent / "secret.key")


def fernet_for(key: bytes) -> Fernet:
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(b"jobalerts vault v1" + key).digest()))


class Vault:
    def __init__(self, store, key: bytes, environ=os.environ):
        self.store = store
        self.fernet = fernet_for(key)
        self.environ = environ
        store.db.executescript(SCHEMA)

    # -- for the UI: no values out
    def status(self) -> list[SecretStatus]:
        stored = {r["name"]: r["updated_at"] for r in self.store.db.execute("SELECT name, updated_at FROM secrets")}
        out = []
        for name, (label, shown) in SECRETS.items():
            source = "env" if self.environ.get(name) else "ui" if name in stored else ""
            value = self.get(name) if shown and source else ""
            out.append(SecretStatus(name, label, bool(source), source, stored.get(name, ""), value))
        return out

    def set(self, name: str, value: str) -> None:
        if name not in SECRETS:
            raise KeyError(name)
        value = value.strip()
        if not value:
            raise ValueError("empty value")
        token = self.fernet.encrypt(value.encode()).decode()
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.store.db.execute("INSERT INTO secrets (name, ciphertext, updated_at) VALUES (?, ?, ?) ON CONFLICT(name) "
                              "DO UPDATE SET ciphertext=excluded.ciphertext, updated_at=excluded.updated_at",
                              (name, token, now))
        log.info("Secret %s saved from the web UI", name)

    def clear(self, name: str) -> None:
        self.store.db.execute("DELETE FROM secrets WHERE name=?", (name,))
        log.info("Secret %s cleared from the web UI", name)

    # -- for the running service only
    def get(self, name: str) -> str:
        if self.environ.get(name):
            return self.environ[name]
        row = self.store.db.execute("SELECT ciphertext FROM secrets WHERE name=?", (name,)).fetchone()
        if not row:
            return ""
        try:
            return self.fernet.decrypt(row["ciphertext"].encode()).decode()
        except InvalidToken:
            log.warning("Stored secret %s can't be decrypted (secret.key changed?); set it again", name)
            return ""

    def values(self) -> list[str]:
        return [v for v in (self.get(n) for n in SECRETS) if len(v) >= MIN_REDACT]

    # -- encrypt other sensitive values (Settings page: salary, home, household)
    def encrypt(self, text: str) -> str:
        return self.fernet.encrypt(text.encode()).decode()

    def decrypt(self, token: str) -> str:
        return self.fernet.decrypt(token.encode()).decode()


_active: Vault | None = None


def install(vault: Vault | None) -> None:
    """Make `secret()` see the stored secrets too (the service does this at start)."""
    global _active
    _active = vault
    _redactor.refresh()


def secret(name: str) -> str:
    """A secret's value: environment / .env first, then the vault. "" when it isn't set."""
    if os.environ.get(name):
        return os.environ[name]
    return _active.get(name) if _active else ""


_KEY_PARAMS = re.compile(r"(?i)\b(app_key|app_id|api_key|apikey|key|token|access_token|password)=([^&\s'\"]+)")


class RedactingFilter(logging.Filter):
    """Blanks secret values out of log records (e.g. an Adzuna URL in an httpx error carries its key)."""

    def __init__(self):
        super().__init__()
        self._values: list[str] = []

    def refresh(self) -> None:
        env = [os.environ.get(n, "") for n in SECRETS]
        stored = _active.values() if _active else []
        self._values = sorted({v for v in env + stored if len(v) >= MIN_REDACT}, key=len, reverse=True)

    def redact(self, text: str) -> str:
        for value in self._values:
            text = text.replace(value, "[redacted]")
        return _KEY_PARAMS.sub(lambda m: f"{m.group(1)}=[redacted]", text)

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # a broken format string: leave it to the handler
            return True
        clean = self.redact(message)
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = self.redact(record.exc_text)
        if clean != message:
            record.msg, record.args = clean, None
        return True


_redactor = RedactingFilter()


def install_log_redaction() -> RedactingFilter:
    """Attach the filter to every root handler (call after logging.basicConfig)."""
    _redactor.refresh()
    for handler in logging.getLogger().handlers:
        if _redactor not in handler.filters:
            handler.addFilter(_redactor)
    return _redactor
