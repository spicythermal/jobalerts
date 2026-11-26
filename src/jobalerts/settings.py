"""Settings made on the web UI, stored in the database and merged on top of config.yaml and
data/config.local.yaml (the UI wins).

Personal values (salary, home, household, contact email) are stored encrypted with the vault's key;
the rest (thresholds, word lists) in plain text. Each field is one dotted config name
("pay.current_home.payment"); FIELDS says how the Settings page shows and reads it.

Each section is either a user's own (situation, filter, alerts: stored under their user id) or the
install's (connections, updates, users: stored under user id 0, set by admins).
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from pydantic import ValidationError

from .config import Config
from .userdata import LOCAL_HEADER, RENAMED, deep_merge, validate_layered

# `settings` (one row per key) is the table of versions before accounts; it's copied once and left
# as it was, so an older version still runs on this database.
SCHEMA = """
CREATE TABLE IF NOT EXISTS settings_by_user (
    user_id    INTEGER NOT NULL,   -- 0: the install's
    key        TEXT NOT NULL,
    value      TEXT NOT NULL,      -- JSON, or vault ciphertext of the JSON when encrypted = 1
    encrypted  INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (user_id, key)
);
"""
INSTALL = 0
# Settings that were each user's and are now the install's (UiSettings._move_to_install).
MOVED_TO_INSTALL = ("notify.server",)
log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    kind: str                 # money | int | number | percent | text | longtext | bool | choice | lines | time | timezone | email
    section: str
    help: str = ""
    sensitive: bool = False   # stored encrypted; never published (MQTT, logs)
    choices: tuple[tuple[str, str], ...] = ()

    @property
    def scope(self) -> str:
        """"user": each user's own; "install": one value for everyone, set by admins."""
        return SCOPE[self.section]


MODELS = (("claude-haiku-5-5", "Claude Haiku 5.5 (cheapest)"), ("claude-sonnet-5-5", "Claude Sonnet 5.5"),
          ("claude-opus-5-5", "Claude Opus 5.5 (most careful, most expensive)"))

SECTIONS = {
    "situation": "Your situation",
    "search": "Filter",
    "alerts": "Scoring and alerts",
    "connections": "Connections and Home Assistant",
    "updates": "Updates",
    "users": "Users",
}
SCOPE = {"situation": "user", "search": "user", "alerts": "user", "connections": "install", "updates": "install",
         "users": "install"}

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
    # -- the Filter page: how jobs reach the AI score
    Field("criteria.mode", "How jobs reach the AI score", "choice", "search",
          "Keywords first: the word lists decide, and the AI takes a cheap second look at what they turn down. "
          "AI only: the word lists are off (except exclude and seniority words); every new job gets the AI's "
          "first look in batches, then the survivors get the full score.",
          choices=(("keywords", "Keywords first, AI second look"), ("ai", "AI only (in stages)"))),
    Field("prescreen.enabled", "AI first look (pre-screen)", "bool", "search",
          "Keywords mode: a second look at what the words turn down. AI-only mode needs it on."),
    Field("prescreen.model", "Model for the first look", "model", "search",
          "Sorts titles 50 at a time, then excerpts 8 at a time: a cheap, fast model is enough.",
          choices=(("", "Same as the full score"), *MODELS)),
    Field("scoring.model", "Model for the full score", "model", "search",
          "Reads each surviving posting against your profile and stories.", choices=MODELS),
    Field("prescreen.guidance", "What the first look should turn down or keep", "longtext", "search",
          "In your own words, e.g. \"Turn down sales, HR and facilities roles. Keep instrument and hardware roles "
          "even with generic titles.\" Your field words and profile are already included."),
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
    Field("criteria.description_exclude", "Exclude when the description says", "lines", "search",
          "Checked after the title passes, e.g. \"active secret clearance\"."),
    # -- scoring and alerts
    Field("scoring.enabled", "AI scoring", "bool", "alerts",
          "Needs an Anthropic API key (Settings → API keys). Without it, alerts use the keywords only."),
    Field("scoring.push_threshold", "Push to the phone at fit score", "int", "alerts", "0–100; default 75."),
    Field("scoring.digest_threshold", "Evening digest at fit score", "int", "alerts", "Below this a match is only stored."),
    Field("scoring.step_up_threshold", "Push a step-up role at", "int", "alerts",
          "A credible promotion is pushed at this step-up score even when the fit is lower; 0 = off."),
    Field("scoring.daily_max_calls", "Most AI calls a day", "int", "alerts", "Scoring and pre-screen together."),
    Field("pay.min_index", "Skip jobs whose pay index is under", "number", "alerts",
          "Checked against the top of the stated range, renting and buying (either passes); 0 = off."),
    Field("notify.timezone", "Time zone", "timezone", "alerts", "For the digest time, e.g. America/Chicago."),
    Field("notify.digest_time", "Evening digest at", "time", "alerts", "HH:MM, your time zone."),
    # -- connections (the install's)
    Field("contact_email", "Contact email", "email", "connections",
          "Sent in the User-Agent so job-board operators can reach the person running this install.", True),
    Field("notify.server", "ntfy server", "text", "connections",
          "https://ntfy.sh or your own, for every user's phone notifications. Each user's topic and token are "
          "under API keys."),
    Field("web.public_url", "Address of this web UI from your phone", "text", "connections",
          "e.g. http://192.168.1.20:8765. Alerts then open the job here; empty = the employer's posting."),
    Field("homeassistant.enabled", "Publish to Home Assistant (MQTT)", "bool", "connections"),
    Field("homeassistant.mqtt_host", "MQTT broker host", "text", "connections",
          "The Home Assistant box's address (Mosquitto add-on). The password is under API keys."),
    Field("homeassistant.mqtt_port", "MQTT port", "int", "connections"),
    Field("homeassistant.mqtt_username", "MQTT user", "text", "connections"),
    Field("web.lan", "Desktop app: other computers on this network can open it", "bool", "connections",
          "Off: only this computer. On: other computers on your network open it at this computer's address "
          "(Settings → Users shows it), port 8765. Takes effect when the app starts again. Servers use web.bind."),
    # -- updates
    Field("update.mode", "When a new version is out", "choice", "updates",
          "Checked once a day. A new version is installed only when an admin clicks Update now. Installing "
          "never overwrites anything of yours, backs up the database first, and goes back if the new version "
          "fails its check.",
          choices=(("notify", "Tell me (push and digest)"), ("off", "Don't check"))),
    Field("update.interval_hours", "Check every (hours)", "int", "updates"),
    # -- users (the install's)
    Field("users.share_keys", "Other users may use this install's Anthropic key", "bool", "users",
          "On: users without their own key are AI-scored with the install's key, and may set their own instead. "
          "Off: a user's AI scoring uses only their own key; without one, they get keyword filtering and the "
          "pay index. Phone notifications are always each user's own."),
    Field("users.shared_daily_max_calls", "Most AI calls a day on the install's key, all other users together",
          "int", "users", "Admins' own scoring doesn't count. Each user's own daily limit applies too."),
]
BY_KEY = {f.key: f for f in FIELDS}
_TIME = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")
_MODEL = re.compile(r"^claude-[a-z0-9.-]{3,60}$")
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
        if field.kind == "model":   # one of the listed models, or another model id set in a config file
            if text not in dict(field.choices) and not _MODEL.match(text):
                raise ValueError
            return text
        if field.kind == "longtext":
            return "\n".join(line.rstrip() for line in (raw or "").strip().splitlines())
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
                                     "email": "Not an email address.", "choice": "Pick one of the options.",
                                     "model": "Pick one of the models."}
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
    """The settings one user sees: the install's (user id 0) and their own. Saving puts each value
    under the id its section belongs to."""

    def __init__(self, store, vault, uid: int = 1):
        self.store, self.vault, self.uid = store, vault, uid
        store.db.executescript(SCHEMA)
        self._copy_old_table()
        self._move_to_install()
        for old, new in RENAMED.items():   # settings a later version renamed keep their saved value
            store.db.execute("UPDATE OR IGNORE settings_by_user SET key=? WHERE key=?", (new, old))

    def _copy_old_table(self) -> None:
        """Once: the single-user `settings` table's values become the install's and user 1's."""
        db = self.store.db
        if db.execute("SELECT 1 FROM settings_by_user LIMIT 1").fetchone() or \
                not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='settings'").fetchone():
            return
        for r in db.execute("SELECT key, value, encrypted, updated_at FROM settings").fetchall():
            key = RENAMED.get(r["key"], r["key"])
            field = BY_KEY.get(key)
            uid = INSTALL if field is not None and field.scope == "install" else 1
            db.execute("INSERT OR IGNORE INTO settings_by_user (user_id, key, value, encrypted, updated_at) "
                       "VALUES (?, ?, ?, ?, ?)", (uid, key, r["value"], r["encrypted"], r["updated_at"]))

    def _move_to_install(self) -> None:
        """Once: settings that became the install's take user 1's saved value. Other users' values stay
        in the database, unused."""
        store, db = self.store, self.store.db
        for key in MOVED_TO_INSTALL:
            if store.get_meta(f"settings.moved.{key}"):
                continue
            rows = db.execute("SELECT user_id, value, encrypted, updated_at FROM settings_by_user "
                              "WHERE key=? AND user_id<>?", (key, INSTALL)).fetchall()
            for r in rows:
                if r["user_id"] == 1:
                    db.execute("INSERT OR IGNORE INTO settings_by_user (user_id, key, value, encrypted, updated_at) "
                               "VALUES (?, ?, ?, ?, ?)", (INSTALL, key, r["value"], r["encrypted"], r["updated_at"]))
                else:
                    log.warning("Settings: %s is now set by admins for everyone (Settings → Connections); "
                                "user %d's own value is no longer used", key, r["user_id"])
            store.set_meta(f"settings.moved.{key}", "1")

    def _owner(self, key: str) -> int:
        field = BY_KEY.get(key)
        return INSTALL if field is not None and field.scope == "install" else self.uid

    def values(self) -> dict[str, object]:
        """Saved values of settings this version knows (by their current names)."""
        return {k: v for k, v in self.all_values().items() if k in BY_KEY}

    def unknown(self) -> list[str]:
        """Saved values of settings this version no longer has (kept, in case you go back)."""
        return sorted(k for k in self.all_values() if k not in BY_KEY)

    def all_values(self) -> dict[str, object]:
        out = {}
        for r in self.store.db.execute("SELECT user_id, key, value, encrypted FROM settings_by_user "
                                       "WHERE user_id IN (?, ?)", (INSTALL, self.uid)):
            if r["user_id"] != self._owner(r["key"]):   # a value saved in the other scope by an older layout
                continue
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
            self.store.db.execute(
                "INSERT INTO settings_by_user (user_id, key, value, encrypted, updated_at) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(user_id, key) DO UPDATE SET value=excluded.value, encrypted=excluded.encrypted, "
                "updated_at=excluded.updated_at", (self._owner(key), key, stored, encrypted, now))

    def reset(self, keys: list[str]) -> None:
        for key in keys:
            self.store.db.execute("DELETE FROM settings_by_user WHERE user_id=? AND key=?", (self._owner(key), key))


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
