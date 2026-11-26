"""SQLite state: every job ever seen, board health, AI usage, suggestions, per-host request stats,
and the web UI's accounts.

Jobs are shared, what each user's pipeline made of them is not:
- `jobs`: one row per posting (what the board says, its fingerprint, the description while a
  pre-screen waits for it) and a shared status: baseline (open at the first poll), duplicate, or seen.
- `user_jobs`: one row per user and job: their filter's decision, AI score, pay index, pushes and
  saved / dismissed / applied.
`Store.user(uid)` is one user's view (`UserJobs`): the same job methods, over both tables. The
Store's own job methods are the first user's (id 1), the install's owner.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

from .models import Job, normalize_company, parse_dt, utcnow

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    key           TEXT PRIMARY KEY,
    source        TEXT NOT NULL,
    board         TEXT NOT NULL,
    job_id        TEXT NOT NULL,
    company       TEXT,
    title         TEXT,
    url           TEXT,
    location      TEXT,
    remote        INTEGER,
    salary        TEXT,
    published_at  TEXT,
    first_seen_at TEXT NOT NULL,
    fingerprint   TEXT,
    status        TEXT NOT NULL,
    reason        TEXT,
    score         INTEGER,
    score_summary TEXT,
    red_flags     TEXT,
    best_resume   TEXT,
    pay_index     REAL,
    pay_low       REAL,
    pay_high      REAL,
    pay_note      TEXT,
    notified_at   TEXT,
    description   TEXT,             -- kept only while status='prescreen' (list APIs give it once)
    buy_index     REAL,             -- pay index for a family that buys (pay_* are renting)
    buy_low       REAL,
    buy_high      REAL,
    step_up       INTEGER,          -- AI: how big a credible career step up the role is, 0-100
    step_up_note  TEXT,
    top_stories   TEXT,             -- JSON [[id, title], ...]: the work stories that best fit the job
    score_basis   TEXT              -- which scoring prompt (stories, profile, model) the score came from
);
CREATE INDEX IF NOT EXISTS jobs_fingerprint ON jobs(fingerprint);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status);

CREATE TABLE IF NOT EXISTS boards (
    key             TEXT PRIMARY KEY,
    first_polled_at TEXT,
    last_ok_at      TEXT,
    last_count      INTEGER,
    failures        INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    last_error_at   TEXT
);

CREATE TABLE IF NOT EXISTS ai_usage (
    day           TEXT PRIMARY KEY,
    calls         INTEGER NOT NULL DEFAULT 0,
    input_tokens  INTEGER NOT NULL DEFAULT 0,  -- uncached input (rows before 2026-10-28 include cache tokens)
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens  INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS suggestions (
    company_key  TEXT PRIMARY KEY,
    company      TEXT NOT NULL,
    matches      INTEGER NOT NULL DEFAULT 0,
    first_seen   TEXT NOT NULL,
    last_seen    TEXT NOT NULL,
    resolved     TEXT,
    reported_at  TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS user_jobs (
    user_id       INTEGER NOT NULL,
    job_key       TEXT NOT NULL,
    status        TEXT NOT NULL,
    reason        TEXT,
    score         INTEGER,
    score_summary TEXT,
    red_flags     TEXT,
    best_resume   TEXT,
    pay_index     REAL,
    pay_low       REAL,
    pay_high      REAL,
    pay_note      TEXT,
    notified_at   TEXT,
    buy_index     REAL,
    buy_low       REAL,
    buy_high      REAL,
    step_up       INTEGER,
    step_up_note  TEXT,
    top_stories   TEXT,
    score_basis   TEXT,
    user_state    TEXT,
    user_state_at TEXT,
    PRIMARY KEY (user_id, job_key)
);
CREATE INDEX IF NOT EXISTS user_jobs_status ON user_jobs(user_id, status);
CREATE INDEX IF NOT EXISTS user_jobs_key ON user_jobs(job_key);

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'user',      -- admin | user
    ver           INTEGER NOT NULL DEFAULT 1,        -- bumped on password, role or access changes: logs out
    must_change   INTEGER NOT NULL DEFAULT 0,        -- a password an admin set: change it at the next login
    disabled      INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    last_login_at TEXT
);

-- AI calls per day and user (ai_usage above is the single-user table of versions before accounts,
-- kept as it was so an older version still runs on this database)
CREATE TABLE IF NOT EXISTS ai_calls (
    day           TEXT NOT NULL,
    user_id       INTEGER NOT NULL,
    calls         INTEGER NOT NULL DEFAULT 0,
    shared_calls  INTEGER NOT NULL DEFAULT 0,       -- made with the install's key by a non-admin
    input_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens  INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, user_id)
);

CREATE TABLE IF NOT EXISTS host_stats (
    day   TEXT NOT NULL,
    host  TEXT NOT NULL,
    name  TEXT NOT NULL,
    count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, host, name)
);
"""

# Statuses: processing, baseline, duplicate, stale, closed, filtered, low_pay, low, digest, alert, alerted, digested, notify_failed
# jobs.status holds only the shared ones (SHARED_STATUSES; "seen" for the rest), user_jobs.status any.
SHARED_STATUSES = ("baseline", "duplicate")
PERSONAL = ("status", "reason", "score", "score_summary", "red_flags", "best_resume", "pay_index", "pay_low",
            "pay_high", "pay_note", "notified_at", "buy_index", "buy_low", "buy_high", "step_up", "step_up_note",
            "top_stories", "score_basis", "user_state", "user_state_at")
_POSTING = ("key", "source", "board", "job_id", "company", "title", "url", "location", "remote", "salary",
            "published_at", "first_seen_at", "fingerprint", "description")
# One user's status of a job: their own row's, else the shared one (rows written before accounts may
# hold other statuses in jobs.status: they count as seen).
_STATUS = ("CASE WHEN u.status IS NOT NULL THEN u.status "
           "WHEN j.status IN ('baseline','duplicate') THEN j.status ELSE 'seen' END")
_VIEW = ("SELECT " + ", ".join(f"j.{c}" for c in _POSTING) + f", {_STATUS} AS status, COALESCE(u.reason, j.reason) AS reason, "
         + ", ".join(f"u.{c}" for c in PERSONAL if c not in ("status", "reason"))
         + " FROM jobs j LEFT JOIN user_jobs u ON u.job_key=j.key AND u.user_id=?")
# Store methods that are the first user's view (the install's owner): Store.update_job(...) etc.
USER_METHODS = {"insert_job", "hold_for_prescreen", "prescreen_queue", "drop_prescreen_queue", "update_job",
                "mark_notified", "digest_jobs", "recent_candidates", "jobs_since", "jobs_by_user_state",
                "matches_by_board", "decisions_since", "get_job", "set_user_state", "count_status", "mark_digested",
                "processed_since", "scored_since", "low_pay_since", "activity_since", "ai_calls_today",
                "record_ai_call", "ai_calls_left"}
# meta values kept per user (others are the install's: heartbeat, update status, failing boards)
USER_META = {"run.last_digest_for", "run.catchup_report", "run.cap_noticed", "web.share_token"}


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat(timespec="seconds") if dt else None


def job_from_row(r: sqlite3.Row) -> Job:
    """A stored job back as a Job (the description is stored only while it waits for the pre-screen;
    otherwise sources re-fetch it when needed)."""
    return Job(r["source"], r["board"], r["job_id"], r["company"] or "", r["title"] or "", r["url"] or "",
               location=r["location"] or "", remote=bool(r["remote"]), published_at=parse_dt(r["published_at"]),
               salary=r["salary"] or "", description=r["description"] or "")


# Bumped when an upgrade changes stored data (column additions below don't need it). Before an
# upgrade step runs, the database is copied to data/backups/ (the last KEEP_BACKUPS are kept).
#   1: everything up to the web UI   2: stored pay notes without your housing figures
#   3: accounts: the password becomes user 1 ("admin"); job decisions, scores and AI calls move to
#      user_jobs / ai_calls as user 1's (the old columns and ai_usage are left as they were)
SCHEMA_VERSION = 3
KEEP_BACKUPS = 5
_HOUSING_FIGURES = re.compile(r"staying in your home(?: at [\d.]+%)?: [^;)]*")


class Store:
    def __init__(self, path: str | Path):
        self._users: dict[int, UserJobs] = {}
        self.path = None if str(path) == ":memory:" else Path(path)
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        existed = bool(self.path and self.path.is_file() and self.path.stat().st_size)
        # autocommit; the web UI's handlers share the connection (same event loop in `run`; a worker
        # thread under the test client), never at the same time.
        self.db = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            log.warning("This database was written by a newer jobalerts (schema %d, this one knows %d); "
                        "nothing is changed, but some of it may not show", version, SCHEMA_VERSION)
        elif existed and version < SCHEMA_VERSION:
            self.backup(f"before-schema-{SCHEMA_VERSION}")
        self.db.executescript(SCHEMA)
        columns = {r["name"] for r in self.db.execute("PRAGMA table_info(jobs)")}
        if "best_resume" not in columns:  # databases created before resume matching
            self.db.execute("ALTER TABLE jobs ADD COLUMN best_resume TEXT")
        if "pay_index" not in columns:  # databases created before the cost-of-living pay index
            self.db.execute("ALTER TABLE jobs ADD COLUMN pay_index REAL")
            self.db.execute("ALTER TABLE jobs ADD COLUMN pay_note TEXT")
        if "pay_low" not in columns:  # databases from the first day of the pay index
            self.db.execute("ALTER TABLE jobs ADD COLUMN pay_low REAL")
            self.db.execute("ALTER TABLE jobs ADD COLUMN pay_high REAL")
        if "description" not in columns:  # databases created before the AI pre-screen
            self.db.execute("ALTER TABLE jobs ADD COLUMN description TEXT")
        for name, kind in (("buy_index", "REAL"), ("buy_low", "REAL"), ("buy_high", "REAL"),
                           ("step_up", "INTEGER"), ("step_up_note", "TEXT"),  # before rent/buy and step-ups
                           ("top_stories", "TEXT"), ("score_basis", "TEXT"),  # before work stories
                           ("user_state", "TEXT"), ("user_state_at", "TEXT")):  # before the web UI's jobs page
            if name not in columns:
                self.db.execute(f"ALTER TABLE jobs ADD COLUMN {name} {kind}")
        usage_columns = {r["name"] for r in self.db.execute("PRAGMA table_info(ai_usage)")}
        for col in ("cache_read_tokens", "cache_write_tokens"):  # databases created before the AI cost sensor
            if col not in usage_columns:
                self.db.execute(f"ALTER TABLE ai_usage ADD COLUMN {col} INTEGER NOT NULL DEFAULT 0")
        if version < 2:  # pay notes go to phone pushes and Home Assistant: no mortgage figures in them
            for r in self.db.execute("SELECT key, pay_note FROM jobs WHERE pay_note LIKE '%staying in your home%'").fetchall():
                self.db.execute("UPDATE jobs SET pay_note=? WHERE key=?",
                                (_HOUSING_FIGURES.sub("staying in your home", r["pay_note"]), r["key"]))
        if version < 3:
            self._accounts_migration()
        # user 1's jobs as one table, for looking into the database by hand (this connection only)
        self.db.execute("CREATE TEMP VIEW IF NOT EXISTS owner_jobs AS " + _VIEW.replace("u.user_id=?", "u.user_id=1"))
        if version < SCHEMA_VERSION:
            self.db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def _accounts_migration(self) -> None:
        """Version 3: what the single user had becomes user 1's."""
        cols = ", ".join(c for c in PERSONAL)
        self.db.execute("BEGIN")
        try:
            self.db.execute(f"""INSERT OR IGNORE INTO user_jobs (user_id, job_key, {cols})
                                SELECT 1, key, {cols} FROM jobs
                                WHERE status NOT IN ('baseline','duplicate') OR user_state IS NOT NULL""")
            self.db.execute("""INSERT OR IGNORE INTO ai_calls (day, user_id, calls, input_tokens, output_tokens,
                                   cache_read_tokens, cache_write_tokens)
                               SELECT day, 1, calls, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens
                               FROM ai_usage""")
            password = self.get_meta("web.password_hash")
            if password and not self.db.execute("SELECT 1 FROM users").fetchone():
                self.db.execute("INSERT INTO users (id, username, password_hash, role, ver, created_at) "
                                "VALUES (1, 'admin', ?, 'admin', 1, ?)", (password, _iso(utcnow())))
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def user(self, uid: int) -> UserJobs:
        """One user's view of the jobs (their decisions, scores, saved jobs, AI calls)."""
        if uid not in self._users:
            self._users[uid] = UserJobs(self, uid)
        return self._users[uid]

    def __getattr__(self, name: str):
        if name in USER_METHODS:     # the first user's (the single user of versions before accounts)
            return getattr(self.user(1), name)
        raise AttributeError(name)

    def backup(self, reason: str) -> Path | None:
        """A consistent copy of the database in data/backups/ (SQLite's online backup); keeps the last
        KEEP_BACKUPS. Done automatically before an upgrade changes stored data."""
        if not self.path:
            return None
        folder = self.path.parent / "backups"
        folder.mkdir(exist_ok=True)
        target = folder / f"{self.path.stem}-{utcnow():%Y%m%d-%H%M%S-%f}-{reason}.db"
        dest = sqlite3.connect(target)
        try:
            self.db.backup(dest)
        finally:
            dest.close()
        for old in sorted(folder.glob(f"{self.path.stem}-*.db"))[:-KEEP_BACKUPS]:
            old.unlink()
        log.info("Database backed up to %s", target)
        return target

    def close(self) -> None:
        self.db.close()

    # ---- small named values (web login, share token, run state) ----
    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        self.db.execute("INSERT INTO meta (key, value, updated_at) VALUES (?, ?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                        (key, value, _iso(utcnow())))

    def delete_meta(self, key: str) -> None:
        self.db.execute("DELETE FROM meta WHERE key=?", (key,))

    # ---- boards ----
    def is_baseline(self, board_key: str) -> bool:
        row = self.db.execute("SELECT first_polled_at FROM boards WHERE key=?", (board_key,)).fetchone()
        return row is None or row["first_polled_at"] is None

    def mark_board_ok(self, board_key: str, count: int) -> None:
        now = _iso(utcnow())
        self.db.execute(
            """INSERT INTO boards(key, first_polled_at, last_ok_at, last_count, failures)
               VALUES(?,?,?,?,0)
               ON CONFLICT(key) DO UPDATE SET last_ok_at=excluded.last_ok_at,
                   last_count=excluded.last_count, failures=0,
                   first_polled_at=COALESCE(boards.first_polled_at, excluded.first_polled_at)""",
            (board_key, now, now, count),
        )

    def mark_board_failed(self, board_key: str, error: str) -> int:
        self.db.execute(
            """INSERT INTO boards(key, failures, last_error, last_error_at) VALUES(?,1,?,?)
               ON CONFLICT(key) DO UPDATE SET failures=boards.failures+1,
                   last_error=excluded.last_error, last_error_at=excluded.last_error_at""",
            (board_key, error[:500], _iso(utcnow())),
        )
        return self.db.execute("SELECT failures FROM boards WHERE key=?", (board_key,)).fetchone()[0]

    def boards(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM boards ORDER BY key").fetchall()

    # ---- jobs (shared) ----
    def known_keys(self, keys: list[str]) -> set[str]:
        known: set[str] = set()
        for i in range(0, len(keys), 500):
            chunk = keys[i:i + 500]
            marks = ",".join("?" * len(chunk))
            known.update(r[0] for r in self.db.execute(f"SELECT key FROM jobs WHERE key IN ({marks})", chunk))
        return known

    def seen_elsewhere(self, job: Job, days: int = 60) -> bool:
        """Same company+title already recorded from a *different* source recently."""
        since = _iso(utcnow() - timedelta(days=days))
        row = self.db.execute(
            "SELECT 1 FROM jobs WHERE fingerprint=? AND source<>? AND first_seen_at>=? LIMIT 1",
            (job.fingerprint, job.source, since),
        ).fetchone()
        return row is not None

    def insert_shared(self, job: Job, status: str = "seen", reason: str = "") -> None:
        """The posting itself (once, whoever sees it first): status baseline, duplicate or seen."""
        self.db.execute(
            """INSERT OR IGNORE INTO jobs(key, source, board, job_id, company, title, url, location, remote,
                   salary, published_at, first_seen_at, fingerprint, status, reason)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (job.key, job.source, job.board, job.job_id, job.company, job.title, job.url, job.location,
             int(job.remote), job.salary, _iso(job.published_at), _iso(utcnow()), job.fingerprint,
             status if status in SHARED_STATUSES else "seen", reason if status in SHARED_STATUSES else ""),
        )

    def insert_jobs(self, jobs: list[Job], status: str) -> None:
        self.db.execute("BEGIN")
        try:
            for job in jobs:
                self.insert_shared(job, status)
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def latency_minutes(self, days: int = 7) -> list[float]:
        """first_seen_at - published_at for jobs we discovered live (not baseline), in minutes."""
        since = _iso(utcnow() - timedelta(days=days))
        rows = self.db.execute(
            """SELECT source, published_at, first_seen_at FROM jobs
               WHERE status NOT IN ('baseline','stale') AND published_at IS NOT NULL AND first_seen_at>=?""",
            (since,)).fetchall()
        out = []
        for r in rows:
            delta = datetime.fromisoformat(r["first_seen_at"]) - datetime.fromisoformat(r["published_at"])
            out.append(delta.total_seconds() / 60)
        return out

    def forget_user(self, uid: int) -> None:
        """A deleted account: its job rows, AI call counts and personal meta values."""
        self.db.execute("DELETE FROM user_jobs WHERE user_id=?", (uid,))
        self.db.execute("DELETE FROM ai_calls WHERE user_id=?", (uid,))
        self.db.execute("DELETE FROM meta WHERE key LIKE ?", (f"user.{uid}.%",))
        self._users.pop(uid, None)

    # ---- AI usage (the whole install) ----
    _USAGE = ("SELECT day, SUM(calls) AS calls, SUM(shared_calls) AS shared_calls, SUM(input_tokens) AS input_tokens, "
              "SUM(output_tokens) AS output_tokens, SUM(cache_read_tokens) AS cache_read_tokens, "
              "SUM(cache_write_tokens) AS cache_write_tokens FROM ai_calls WHERE day>=? {extra} GROUP BY day ORDER BY day")

    def ai_usage_since(self, day: str) -> list[sqlite3.Row]:
        """Daily AI usage rows from `day` (YYYY-MM-DD, UTC) on, all users together."""
        return self.db.execute(self._USAGE.format(extra=""), (day,)).fetchall()

    def ai_usage(self, days: int = 7) -> list[sqlite3.Row]:
        return self.ai_usage_since((utcnow().date() - timedelta(days=days)).isoformat())

    def shared_calls_today(self) -> int:
        row = self.db.execute("SELECT SUM(shared_calls) FROM ai_calls WHERE day=?",
                              (utcnow().date().isoformat(),)).fetchone()
        return row[0] or 0

    # ---- suggestions (companies seen via aggregators that aren't watched) ----
    def add_suggestion(self, company: str) -> None:
        key = normalize_company(company)
        if not key:
            return
        now = _iso(utcnow())
        self.db.execute(
            """INSERT INTO suggestions(company_key, company, matches, first_seen, last_seen) VALUES(?,?,1,?,?)
               ON CONFLICT(company_key) DO UPDATE SET matches=matches+1, last_seen=excluded.last_seen""",
            (key, company, now, now),
        )

    def pending_suggestions(self, limit: int) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM suggestions WHERE reported_at IS NULL ORDER BY matches DESC, last_seen DESC LIMIT ?",
            (limit,)).fetchall()

    def set_suggestion_resolved(self, company_key: str, resolved: str) -> None:
        self.db.execute("UPDATE suggestions SET resolved=? WHERE company_key=?", (resolved, company_key))

    def mark_suggestions_reported(self, company_keys: list[str]) -> None:
        now = _iso(utcnow())
        for key in company_keys:
            self.db.execute("UPDATE suggestions SET reported_at=? WHERE company_key=?", (now, key))

    # ---- per-host request stats ----
    def add_host_stats(self, stats: dict[str, Counter]) -> None:
        day = utcnow().date().isoformat()
        for host, counter in stats.items():
            for name, count in counter.items():
                self.db.execute(
                    """INSERT INTO host_stats(day, host, name, count) VALUES(?,?,?,?)
                       ON CONFLICT(day, host, name) DO UPDATE SET count=count+excluded.count""",
                    (day, host, name, count),
                )

    def host_stats(self, days: int = 1) -> dict[str, Counter]:
        since = (utcnow().date() - timedelta(days=days - 1)).isoformat()
        out: dict[str, Counter] = {}
        for r in self.db.execute("SELECT host, name, SUM(count) FROM host_stats WHERE day>=? GROUP BY host, name",
                                 (since,)):
            out.setdefault(r[0], Counter())[r[1]] = r[2]
        return out


class UserJobs:
    """One user's view of the jobs: the posting from `jobs`, their decision, score, pay index and
    saved / applied state from `user_jobs`. Shared methods (boards, suggestions, host stats) pass
    through to the Store."""

    PAGE_STATUSES = ("alert", "alerted", "digest", "digested", "notify_failed", "baseline", "low", "low_pay")

    def __init__(self, store: Store, uid: int):
        self.base, self.uid, self.db = store, uid, store.db
        self.shared_key = False      # AI calls go to the install's key on a non-admin's behalf (counted for the shared cap)
        self.shared_cap: int | None = None

    def __getattr__(self, name: str):
        return getattr(self.base, name)

    def _view(self, where: str, order: str = "") -> str:
        return f"SELECT * FROM ({_VIEW}) WHERE {where} {order}"

    # ---- meta values: some are per user
    def meta_key(self, key: str) -> str:
        return key if self.uid == 1 or key not in USER_META else f"user.{self.uid}.{key}"

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        return self.base.get_meta(self.meta_key(key), default)

    def set_meta(self, key: str, value: str) -> None:
        self.base.set_meta(self.meta_key(key), value)

    def delete_meta(self, key: str) -> None:
        self.base.delete_meta(self.meta_key(key))

    # ---- writing
    def insert_job(self, job: Job, status: str, reason: str = "") -> None:
        """The posting (if new) and this user's decision on it (if they have none yet). Baseline and
        duplicate are the posting's, everyone's."""
        self.base.insert_shared(job, status, reason)
        if status not in SHARED_STATUSES:
            self.db.execute("INSERT OR IGNORE INTO user_jobs(user_id, job_key, status, reason) VALUES(?,?,?,?)",
                            (self.uid, job.key, status, reason))

    def _ensure(self, key: str) -> None:
        """This user's row for a job they have none for yet (a baseline job saved or scored)."""
        self.db.execute("""INSERT OR IGNORE INTO user_jobs(user_id, job_key, status)
                            SELECT ?, j.key, CASE WHEN j.status IN ('baseline','duplicate') THEN j.status ELSE 'seen' END
                            FROM jobs j WHERE j.key=?""", (self.uid, key))

    def hold_for_prescreen(self, job: Job, reason: str) -> None:
        self.insert_job(job, "prescreen", reason)
        self.db.execute("UPDATE jobs SET description=? WHERE key=?", (job.description or None, job.key))

    def prescreen_queue(self) -> list[sqlite3.Row]:
        return self.db.execute(self._view("status='prescreen'", "ORDER BY first_seen_at"), (self.uid,)).fetchall()

    def _drop_description(self, key: str) -> None:
        """The description is kept only while someone's pre-screen waits for the job."""
        self.db.execute("UPDATE jobs SET description=NULL WHERE key=? AND description IS NOT NULL AND NOT EXISTS "
                        "(SELECT 1 FROM user_jobs WHERE job_key=? AND status='prescreen')", (key, key))

    def drop_prescreen_queue(self) -> int:
        """Pre-screen turned off: the waiting jobs count as filtered, as they would have been without it."""
        keys = [r[0] for r in self.db.execute("SELECT job_key FROM user_jobs WHERE user_id=? AND status='prescreen'",
                                              (self.uid,))]
        self.db.execute("UPDATE user_jobs SET status='filtered' WHERE user_id=? AND status='prescreen'", (self.uid,))
        for key in keys:
            self._drop_description(key)
        return len(keys)

    def update_job(self, key: str, *, status: str, reason: str | None = None, score: int | None = None,
                   score_summary: str | None = None, red_flags: str | None = None,
                   best_resume: str | None = None, pay_index: float | None = None,
                   pay_low: float | None = None, pay_high: float | None = None,
                   pay_note: str | None = None, buy_index: float | None = None, buy_low: float | None = None,
                   buy_high: float | None = None, step_up: int | None = None,
                   step_up_note: str | None = None, top_stories: str | None = None,
                   score_basis: str | None = None) -> None:
        self._ensure(key)
        self.db.execute(
            """UPDATE user_jobs SET status=?, reason=COALESCE(?, reason), score=COALESCE(?, score),
                   score_summary=COALESCE(?, score_summary), red_flags=COALESCE(?, red_flags),
                   best_resume=COALESCE(?, best_resume), pay_index=COALESCE(?, pay_index),
                   pay_low=COALESCE(?, pay_low), pay_high=COALESCE(?, pay_high),
                   pay_note=COALESCE(?, pay_note), buy_index=COALESCE(?, buy_index),
                   buy_low=COALESCE(?, buy_low), buy_high=COALESCE(?, buy_high),
                   step_up=COALESCE(?, step_up), step_up_note=COALESCE(?, step_up_note),
                   top_stories=COALESCE(?, top_stories), score_basis=COALESCE(?, score_basis)
               WHERE user_id=? AND job_key=?""",
            (status, reason, score, score_summary, red_flags, best_resume, pay_index, pay_low, pay_high,
             pay_note, buy_index, buy_low, buy_high, step_up, step_up_note, top_stories, score_basis, self.uid, key),
        )
        if status != "prescreen":
            self._drop_description(key)

    def mark_notified(self, key: str, status: str = "alerted") -> None:
        self._ensure(key)
        self.db.execute("UPDATE user_jobs SET status=?, notified_at=? WHERE user_id=? AND job_key=?",
                        (status, _iso(utcnow()), self.uid, key))

    def mark_digested(self, keys: list[str]) -> None:
        for key in keys:
            self.mark_notified(key, "digested")

    def set_user_state(self, key: str, state: str | None) -> bool:
        """saved | dismissed | applied, or None to clear. False if there's no such job."""
        if not self.db.execute("SELECT 1 FROM jobs WHERE key=?", (key,)).fetchone():
            return False
        self._ensure(key)
        self.db.execute("UPDATE user_jobs SET user_state=?, user_state_at=? WHERE user_id=? AND job_key=?",
                        (state or None, _iso(utcnow()) if state else None, self.uid, key))
        return True

    # ---- reading
    def get_job(self, key: str) -> sqlite3.Row | None:
        return self.db.execute(self._view("key=?"), (self.uid, key)).fetchone()

    def _statuses(self, statuses, extra: str = "", args: tuple = (), order: str = "") -> list[sqlite3.Row]:
        marks = ",".join("?" * len(statuses))
        where = f"status IN ({marks})" + (f" AND {extra}" if extra else "")
        return self.db.execute(self._view(where, order), (self.uid, *statuses, *args)).fetchall()

    def digest_jobs(self, limit: int) -> list[sqlite3.Row]:
        return self._statuses(("digest", "notify_failed"), order="ORDER BY COALESCE(score, 0) DESC, first_seen_at DESC "
                              f"LIMIT {int(limit)}")

    def recent_candidates(self, since: datetime) -> list[sqlite3.Row]:
        """Keyword matches plus silently recorded baseline jobs posted since `since` (the caller re-filters
        baseline rows, which were stored without filtering)."""
        return self._statuses(("alert", "alerted", "digest", "digested", "notify_failed", "baseline"),
                              "COALESCE(published_at, first_seen_at)>=?", (_iso(since),))

    def jobs_since(self, since: datetime, statuses: tuple[str, ...] = PAGE_STATUSES) -> list[sqlite3.Row]:
        return self._statuses(statuses, "COALESCE(published_at, first_seen_at)>=?", (_iso(since),))

    def jobs_by_user_state(self, state: str) -> list[sqlite3.Row]:
        return self.db.execute(self._view("user_state=?", "ORDER BY user_state_at DESC"), (self.uid, state)).fetchall()

    def matches_by_board(self, since: datetime) -> dict[str, int]:
        """Keyword matches first seen since `since`, per board key (source:board)."""
        rows = self.db.execute(
            """SELECT j.source || ':' || j.board AS k, COUNT(*) AS n FROM user_jobs u JOIN jobs j ON j.key=u.job_key
               WHERE u.user_id=? AND u.status IN ('alert','alerted','digest','digested','notify_failed','low','low_pay')
               AND j.first_seen_at>=? GROUP BY k""", (self.uid, _iso(since))).fetchall()
        return {r["k"]: r["n"] for r in rows}

    def decisions_since(self, since: datetime) -> list[sqlite3.Row]:
        """Every job first seen since `since` that this user's filter decided on (plus baselines and
        duplicates), newest first (the Filter page). No descriptions: they're stored only while a job
        waits for the pre-screen."""
        cols = ", ".join(c for c in _POSTING if c != "description")
        return self.db.execute(
            f"""SELECT {cols}, status, reason, score, NULL AS description FROM ({_VIEW})
                WHERE first_seen_at>=? AND status<>'seen' ORDER BY first_seen_at DESC""",
            (self.uid, _iso(since))).fetchall()

    def count_status(self, statuses: tuple[str, ...]) -> int:
        marks = ",".join("?" * len(statuses))
        return self.db.execute(f"SELECT COUNT(*) FROM ({_VIEW}) WHERE status IN ({marks})",
                               (self.uid, *statuses)).fetchone()[0]

    def processed_since(self, since: datetime) -> list[sqlite3.Row]:
        """Matches that already went through the pipeline (not baseline), posted since `since`."""
        return self._statuses(("alert", "alerted", "digest", "digested", "notify_failed", "low_pay"),
                              "COALESCE(published_at, first_seen_at)>=?", (_iso(since),))

    def scored_since(self, since: datetime) -> list[sqlite3.Row]:
        """Matches the AI already scored, posted since `since` (for re-scoring; low_pay jobs aren't scored)."""
        return self._statuses(("alert", "alerted", "digest", "digested", "notify_failed", "low"),
                              "score IS NOT NULL AND COALESCE(published_at, first_seen_at)>=?", (_iso(since),))

    def low_pay_since(self, since: datetime) -> list[sqlite3.Row]:
        return self._statuses(("low_pay",), "first_seen_at>=?", (_iso(since),),
                              "ORDER BY MAX(COALESCE(pay_index, 0), COALESCE(buy_index, 0)) DESC")

    def activity_since(self, since: datetime) -> Counter:
        rows = self.db.execute(f"SELECT status, COUNT(*) FROM ({_VIEW}) WHERE first_seen_at>=? GROUP BY status",
                               (self.uid, _iso(since)))
        return Counter({r[0]: r[1] for r in rows})

    # ---- AI calls: this user's
    def ai_calls_today(self) -> int:
        row = self.db.execute("SELECT calls FROM ai_calls WHERE day=? AND user_id=?",
                              (utcnow().date().isoformat(), self.uid)).fetchone()
        return row[0] if row else 0

    def ai_calls_left(self, cap: int) -> int:
        """Calls left today under this user's cap and, on the install's key, the shared cap."""
        left = cap - self.ai_calls_today()
        if self.shared_key and self.shared_cap is not None:
            left = min(left, self.shared_cap - self.base.shared_calls_today())
        return max(0, left)

    def record_ai_call(self, input_tokens: int, output_tokens: int, cache_read_tokens: int = 0,
                       cache_write_tokens: int = 0) -> None:
        self.db.execute(
            """INSERT INTO ai_calls(day, user_id, calls, shared_calls, input_tokens, output_tokens, cache_read_tokens,
                   cache_write_tokens) VALUES(?,?,1,?,?,?,?,?)
               ON CONFLICT(day, user_id) DO UPDATE SET calls=calls+1, shared_calls=shared_calls+excluded.shared_calls,
                   input_tokens=input_tokens+excluded.input_tokens, output_tokens=output_tokens+excluded.output_tokens,
                   cache_read_tokens=cache_read_tokens+excluded.cache_read_tokens,
                   cache_write_tokens=cache_write_tokens+excluded.cache_write_tokens""",
            (utcnow().date().isoformat(), self.uid, int(self.shared_key), input_tokens, output_tokens,
             cache_read_tokens, cache_write_tokens),
        )

    def ai_usage_since(self, day: str) -> list[sqlite3.Row]:
        return self.db.execute(Store._USAGE.format(extra="AND user_id=?"), (day, self.uid)).fetchall()

    def ai_usage(self, days: int = 7) -> list[sqlite3.Row]:
        return self.ai_usage_since((utcnow().date() - timedelta(days=days)).isoformat())


def stories_from_row(row) -> list[tuple[str, str]]:
    """The job's best-fitting work stories as (id, title), best first."""
    try:
        return [tuple(pair) for pair in json.loads(row["top_stories"] or "[]")]
    except (ValueError, TypeError):
        return []
