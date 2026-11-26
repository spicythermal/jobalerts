"""Settings made on the web UI, stored in the database and merged on top of config.yaml and
data/config.local.yaml (the UI wins).

Personal values (salary, home, household, contact email) are stored encrypted with the vault's key;
the rest (thresholds, word lists) in plain text. Each field is one dotted config name
("pay.current_home.payment"); FIELDS says how the Settings page shows and reads it.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from pydantic import ValidationError

from .config import Config
from .userdata import LOCAL_HEADER, RENAMED, deep_merge, validate_layered

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,      -- JSON, or vault ciphertext of the JSON when encrypted = 1
    encrypted  INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    kind: str                 # money | int | number | percent | text | bool | choice | lines | time | timezone | email
    section: str
    help: str = ""
    sensitive: bool = False   # stored encrypted; never published (MQTT, logs)
    choices: tuple[tuple[str, str], ...] = ()


SECTIONS = {
    "situation": "Your situation",
    "search": "What to look for",
    "alerts": "Scoring and alerts",
    "connections": "Notifications and Home Assistant",
}

FIELDS: list[Field] = [
    # -- where you are now: the pay index compares every job with this
    Field("pay.reference_salary", "Current base salary", "money", "situation",
          "Your base pay now, in US dollars a year. The pay index calls this 1.00 where you live.", True),
    Field("pay.reference_location", "Where you live", "text", "situation",
          "A city and state (\"Denver, CO\"), a state, or a country. Remote jobs use it too.", True),
    Field("pay.current_home.payment", "Monthly housing payment", "money", "situation",
          "Mortgage payment including escrowed tax and insurance, or rent. 0 = compare with market rent instead.", True),
    Field("pay.current_home.principal", "Of which principal", "money", "situation",
          "The part of the payment that pays down the loan (savings, not cost). 0 if you rent.", True),
    Field("pay.current_home.upkeep", "Upkeep per month", "money", "situation",
          "Repairs and maintenance, roughly 1% of the home's value a year, per month.", True),
    Field("pay.current_home.rate", "Mortgage rate (%)", "number", "situation",
          "Your locked-in rate, e.g. 4.5. Shown in explanations; buying elsewhere means today's rate.", True),
    Field("household.filing", "Filing status", "choice", "situation",
          "For the taxes in the pay index. Single with dependents is worked out as head of household.", True,
          choices=(("single", "Single"), ("joint", "Married filing jointly (one earner)"))),
    Field("household.children", "Children under 17", "int", "situation", "0–4 (child tax credit).", True),
    Field("household.other_dependents", "Other dependents (17 or older)", "int", "situation", "0 or 1.", True),
    Field("household.k401_rate", "Pre-tax 401(k) (% of base pay)", "percent", "situation",
          "e.g. 6. That money still counts as yours; it only lowers income tax.", True),
    Field("intent.move", "Stay or move", "choice", "situation",
          "Stay: jobs that need a move are dropped. Open: they must clear the move pay floor below. "
          "Anywhere: moves are held to the same pay floor as local jobs. The AI is told too.",
          choices=(("stay", "Stay where I live"), ("open", "Open to moving for the right role"),
                   ("any", "Happy to move anywhere"))),
    Field("pay.move_min_index", "Pay floor for a move", "number", "situation",
          "With \"open\": a job in another metro needs at least this pay index (renting or buying), e.g. 1.15."),
    Field("contact_email", "Contact email", "email", "situation",
          "Sent in the User-Agent so job-board operators can reach you about your traffic.", True),
    # -- what to look for
    Field("criteria.title_field", "Field words", "lines", "search",
          "The work itself (plasma, optics, machine learning). One per line; * at either end runs into longer "
          "words. A title with one passes, even over the soft excludes."),
    Field("criteria.title_include", "Role words", "lines", "search",
          "The kind of role (scientist, engineering manager). A title passes with a field word or a role word."),
    Field("criteria.title_exclude", "Always exclude", "lines", "search", "Any of these drops the job."),
    Field("criteria.title_exclude_soft", "Exclude unless a field word", "lines", "search",
          "Drops the job unless the title also has a field word (\"clinical\" drops Clinical Specialist, keeps Clinical Physicist)."),
    Field("criteria.seniority_exclude", "Seniority to skip", "lines", "search", "Intern, junior, ..."),
    Field("criteria.locations", "Only these places", "lines", "search",
          "Leave empty for anywhere (the pay index and AI rank by place). Otherwise a job's location must contain one."),
    Field("criteria.allow_remote", "Remote jobs pass the place check", "bool", "search"),
    Field("criteria.max_age_days", "Ignore jobs posted more than this many days ago", "int", "search"),
    # -- scoring and alerts
    Field("scoring.enabled", "AI scoring", "bool", "alerts",
          "Needs an Anthropic API key (Settings → API keys). Without it, alerts use the keywords only."),
    Field("scoring.push_threshold", "Push to the phone at fit score", "int", "alerts", "0–100; default 75."),
    Field("scoring.digest_threshold", "Evening digest at fit score", "int", "alerts", "Below this a match is only stored."),
    Field("scoring.step_up_threshold", "Push a step-up role at", "int", "alerts",
          "A credible promotion is pushed at this step-up score even when the fit is lower; 0 = off."),
    Field("scoring.daily_max_calls", "Most AI calls a day", "int", "alerts", "Scoring and pre-screen together."),
    Field("prescreen.enabled", "AI second look at jobs the keywords turn down", "bool", "alerts"),
    Field("pay.min_index", "Skip jobs whose pay index is under", "number", "alerts",
          "Checked against the top of the stated range, renting and buying (either passes); 0 = off."),
    Field("notify.timezone", "Time zone", "timezone", "alerts", "For the digest time, e.g. America/Chicago."),
    Field("notify.digest_time", "Evening digest at", "time", "alerts", "HH:MM, your time zone."),
    # -- connections
    Field("notify.server", "ntfy server", "text", "connections",
          "https://ntfy.sh or your own. The topic and token are under API keys."),
    Field("web.public_url", "Address of this web UI from your phone", "text", "connections",
          "e.g. http://192.168.1.20:8765. Alerts then open the job here; empty = the employer's posting."),
    Field("homeassistant.enabled", "Publish to Home Assistant (MQTT)", "bool", "connections"),
    Field("homeassistant.mqtt_host", "MQTT broker host", "text", "connections",
          "The Home Assistant box's address (Mosquitto add-on). The password is under API keys."),
    Field("homeassistant.mqtt_port", "MQTT port", "int", "connections"),
    Field("homeassistant.mqtt_username", "MQTT user", "text", "connections"),
]
BY_KEY = {f.key: f for f in FIELDS}
_TIME = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class FieldError(ValueError):
    def __init__(self, key: str, message: str):
        super().__init__(message)
        self.key = key


def parse(field: Field, raw: str | None):
    """A form value -> the config value. Raises FieldError with a message for the page."""
    text = (raw or "").strip()
    try:
        if field.kind == "bool":
            return raw is not None and text not in ("", "0", "false", "off")
        if field.kind == "money":
            t = text.replace("$", "").replace(",", "").replace(" ", "").lower()
            mult = 1000 if t.endswith("k") else 1
            value = float(t.rstrip("k") or "0") * mult
            if value < 0:
                raise ValueError
            return value
        if field.kind == "int":
            return int(text)
        if field.kind == "number":
            return float(text or "0")
        if field.kind == "percent":
            value = float(text.rstrip("%").strip() or "0") / 100
            if not 0 <= value <= 1:
                raise ValueError
            return round(value, 4)
        if field.kind == "lines":
            return [line.strip() for line in (raw or "").splitlines() if line.strip()]
        if field.kind == "time":
            if not _TIME.match(text):
                raise ValueError
            h, m = text.split(":")
            return f"{int(h):02d}:{m}"
        if field.kind == "timezone":
            ZoneInfo(text)
            return text
        if field.kind == "email":
            if text and not _EMAIL.match(text):
                raise ValueError
            return text
        if field.kind == "choice":
            if text not in dict(field.choices):
                raise ValueError
            return text
        return text
    except (ValueError, KeyError, OSError):
        raise FieldError(field.key, {"money": "Enter an amount, e.g. 155000 or 155K.", "int": "Enter a whole number.",
                                     "number": "Enter a number.", "percent": "Enter a percentage, e.g. 6.",
                                     "time": "Enter a time as HH:MM.",
                                     "timezone": "Not a known time zone (e.g. America/Denver).",
                                     "email": "Not an email address.", "choice": "Pick one of the options."}
                         .get(field.kind, "Not a valid value.")) from None


def get_path(tree: dict, dotted: str):
    for part in dotted.split("."):
        if not isinstance(tree, dict) or part not in tree:
            return None
        tree = tree[part]
    return tree


def nest(dotted: str, value) -> dict:
    out: dict = value
    for part in reversed(dotted.split(".")):
        out = {part: out}
    return out


def format_value(field: Field, value) -> str:
    """The config value as the form shows it."""
    if value is None:
        return ""
    if field.kind == "lines":
        return "\n".join(value)
    if field.kind == "money":
        return f"{value:,.0f}"
    if field.kind == "number":
        return f"{value:g}"
    if field.kind == "percent":
        return f"{value * 100:g}"
    return str(value)


class UiSettings:
    def __init__(self, store, vault):
        self.store, self.vault = store, vault
        store.db.executescript(SCHEMA)
        for old, new in RENAMED.items():   # settings a later version renamed keep their saved value
            if not store.db.execute("SELECT 1 FROM settings WHERE key=?", (new,)).fetchone():
                store.db.execute("UPDATE settings SET key=? WHERE key=?", (new, old))

    def values(self) -> dict[str, object]:
        """Saved values of settings this version knows (by their current names)."""
        return {k: v for k, v in self.all_values().items() if k in BY_KEY}

    def unknown(self) -> list[str]:
        """Saved values of settings this version no longer has (kept, in case you go back)."""
        return sorted(k for k in self.all_values() if k not in BY_KEY)

    def all_values(self) -> dict[str, object]:
        out = {}
        for r in self.store.db.execute("SELECT key, value, encrypted FROM settings"):
            text = self.vault.decrypt(r["value"]) if r["encrypted"] else r["value"]
            out[r["key"]] = json.loads(text)
        return out

    def overlay(self) -> dict:
        tree: dict = {}
        for key, value in self.values().items():
            tree = deep_merge(tree, nest(key, value))
        return tree

    def save(self, values: dict[str, object]) -> None:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for key, value in values.items():
            field = BY_KEY[key]
            text = json.dumps(value)
            stored, encrypted = (self.vault.encrypt(text), 1) if field.sensitive else (text, 0)
            self.store.db.execute("INSERT INTO settings (key, value, encrypted, updated_at) VALUES (?, ?, ?, ?) "
                                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value, encrypted=excluded.encrypted, "
                                  "updated_at=excluded.updated_at", (key, stored, encrypted, now))

    def reset(self, keys: list[str]) -> None:
        for key in keys:
            self.store.db.execute("DELETE FROM settings WHERE key=?", (key,))


def apply(base: Config, overlay: dict, strict: bool = False) -> Config:
    """`base` (config.yaml + config.local.yaml) with the UI's settings on top. A saved value this
    version can't use is dropped and listed in cfg.config_problems instead of losing them all; with
    `strict` (saving a form) an invalid value raises ValidationError so the form can say why."""
    data = base.model_dump(exclude={"companies", "config_dir", "value_sources", "config_problems"})
    if strict:
        cfg, problems = Config.model_validate(deep_merge(data, overlay)), []
    else:
        cfg, problems, overlay = validate_layered(Config, data, overlay, "the Settings page")
    cfg.config_dir, cfg.companies = base.config_dir, base.companies
    ui_keys = {key for key in BY_KEY if get_path(overlay, key) is not None}
    cfg.value_sources = {**base.value_sources, **{k: "ui" for k in ui_keys}}
    cfg.config_problems = base.config_problems + problems
    return cfg


def errors_by_field(e: ValidationError) -> dict[str, str]:
    out = {}
    for err in e.errors():
        key = ".".join(str(p) for p in err["loc"])
        out[key] = err["msg"]
    return out


def move_out_of_local_file(local_path: Path, keys: list[str]) -> list[str]:
    """Remove these settings from config.local.yaml (they're now stored encrypted). Returns what was removed."""
    if not local_path.is_file():
        return []
    tree = yaml.safe_load(local_path.read_text(encoding="utf-8")) or {}
    removed = []
    for key in keys:
        parts = key.split(".")
        node = tree
        for part in parts[:-1]:
            node = node.get(part) if isinstance(node, dict) else None
        if isinstance(node, dict) and parts[-1] in node:
            del node[parts[-1]]
            removed.append(key)

    def prune(d: dict) -> dict:
        return {k: prune(v) if isinstance(v, dict) else v for k, v in d.items()
                if not (isinstance(v, dict) and not prune(v))}
    tree = prune(tree)
    tmp = local_path.with_suffix(".tmp")
    tmp.write_text(LOCAL_HEADER + "\n" + (yaml.safe_dump(tree, sort_keys=False, allow_unicode=True, width=110)
                                          if tree else ""), encoding="utf-8")
    tmp.replace(local_path)
    return removed
