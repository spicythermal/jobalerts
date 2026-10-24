"""SQLite state: every job ever seen, board health, AI usage, suggestions, per-host request stats."""
from __future__ import annotations

import sqlite3
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

from .models import Job, normalize_company, utcnow

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
    notified_at   TEXT
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
    input_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0
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

CREATE TABLE IF NOT EXISTS host_stats (
    day   TEXT NOT NULL,
    host  TEXT NOT NULL,
    name  TEXT NOT NULL,
    count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, host, name)
);
"""

# Statuses: processing, baseline, duplicate, stale, filtered, low, digest, alert, alerted, digested, notify_failed


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat(timespec="seconds") if dt else None


class Store:
    def __init__(self, path: str | Path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), isolation_level=None)  # autocommit
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        columns = {r["name"] for r in self.db.execute("PRAGMA table_info(jobs)")}
        if "best_resume" not in columns:  # databases created before resume matching
            self.db.execute("ALTER TABLE jobs ADD COLUMN best_resume TEXT")

    def close(self) -> None:
        self.db.close()

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

    # ---- jobs ----
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

    def insert_job(self, job: Job, status: str, reason: str = "") -> None:
        self.db.execute(
            """INSERT OR IGNORE INTO jobs(key, source, board, job_id, company, title, url, location, remote,
                   salary, published_at, first_seen_at, fingerprint, status, reason)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (job.key, job.source, job.board, job.job_id, job.company, job.title, job.url, job.location,
             int(job.remote), job.salary, _iso(job.published_at), _iso(utcnow()), job.fingerprint,
             status, reason),
        )

    def insert_jobs(self, jobs: list[Job], status: str) -> None:
        self.db.execute("BEGIN")
        try:
            for job in jobs:
                self.insert_job(job, status)
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def update_job(self, key: str, *, status: str, reason: str | None = None, score: int | None = None,
                   score_summary: str | None = None, red_flags: str | None = None,
                   best_resume: str | None = None) -> None:
        self.db.execute(
            """UPDATE jobs SET status=?, reason=COALESCE(?, reason), score=COALESCE(?, score),
                   score_summary=COALESCE(?, score_summary), red_flags=COALESCE(?, red_flags),
                   best_resume=COALESCE(?, best_resume) WHERE key=?""",
            (status, reason, score, score_summary, red_flags, best_resume, key),
        )

    def mark_notified(self, key: str, status: str = "alerted") -> None:
        self.db.execute("UPDATE jobs SET status=?, notified_at=? WHERE key=?", (status, _iso(utcnow()), key))

    def digest_jobs(self, limit: int) -> list[sqlite3.Row]:
        return self.db.execute(
            """SELECT * FROM jobs WHERE status IN ('digest','notify_failed')
               ORDER BY COALESCE(score, 0) DESC, first_seen_at DESC LIMIT ?""", (limit,)).fetchall()

    def recent_candidates(self, since: datetime) -> list[sqlite3.Row]:
        """Keyword matches plus silently recorded baseline jobs posted since `since` (the caller re-filters
        baseline rows, which were stored without filtering)."""
        return self.db.execute(
            """SELECT * FROM jobs WHERE status IN ('alert','alerted','digest','digested','notify_failed','baseline')
               AND COALESCE(published_at, first_seen_at)>=?""", (_iso(since),)).fetchall()

    def count_status(self, statuses: tuple[str, ...]) -> int:
        marks = ",".join("?" * len(statuses))
        return self.db.execute(f"SELECT COUNT(*) FROM jobs WHERE status IN ({marks})", statuses).fetchone()[0]

    def mark_digested(self, keys: list[str]) -> None:
        for key in keys:
            self.mark_notified(key, "digested")

    def activity_since(self, since: datetime) -> Counter:
        rows = self.db.execute(
            "SELECT status, COUNT(*) FROM jobs WHERE first_seen_at>=? GROUP BY status", (_iso(since),))
        return Counter({r[0]: r[1] for r in rows})

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

    # ---- AI usage ----
    def ai_calls_today(self) -> int:
        row = self.db.execute("SELECT calls FROM ai_usage WHERE day=?", (utcnow().date().isoformat(),)).fetchone()
        return row[0] if row else 0

    def record_ai_call(self, input_tokens: int, output_tokens: int) -> None:
        self.db.execute(
            """INSERT INTO ai_usage(day, calls, input_tokens, output_tokens) VALUES(?,1,?,?)
               ON CONFLICT(day) DO UPDATE SET calls=calls+1, input_tokens=input_tokens+excluded.input_tokens,
                   output_tokens=output_tokens+excluded.output_tokens""",
            (utcnow().date().isoformat(), input_tokens, output_tokens),
        )

    def ai_usage(self, days: int = 7) -> list[sqlite3.Row]:
        since = (utcnow().date() - timedelta(days=days)).isoformat()
        return self.db.execute("SELECT * FROM ai_usage WHERE day>=? ORDER BY day", (since,)).fetchall()

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
