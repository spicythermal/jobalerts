"""Run anywhere: the same `jobalerts run` on a 24/7 server and on a PC that's only on sometimes.

The service writes a heartbeat to the database every minute. When it starts after a gap (the PC
was off) or notices that wall-clock time jumped between heartbeats (the PC slept), it catches up
once: every board is polled right away, aggregator searches look back over the gap, the pre-screen
runs at the end, and instead of a push per job it sends one summary plus the best few, and keeps
a report for the jobs page. A digest that was due while it was off is sent once. A server that
never stops never sees a gap, so nothing changes there.

Some things can't be caught up: a job posted and taken down while the PC was off is never seen,
and boards that list only their newest jobs (Workday's first page) may have moved past some.

Only one `jobalerts run` may use a database at a time (a lock file next to it).
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

HEARTBEAT, LAST_DIGEST, REPORT = "run.heartbeat_at", "run.last_digest_for", "run.catchup_report"
FAILURE_ALERTED, CAP_NOTICED = "run.failure_alerted", "run.cap_noticed"
HONEST_GAPS = ("Jobs posted and taken down while this computer was off can't be seen, and boards that list only "
               "their newest jobs (e.g. Workday's first page) may have moved past some.")


def now() -> datetime:
    return datetime.now(timezone.utc)


def human(seconds: float) -> str:
    minutes = seconds / 60
    if minutes < 90:
        return f"{minutes:.0f} minutes"
    hours = minutes / 60
    return f"{hours:.0f} hours" if hours < 48 else f"{hours / 24:.1f} days"


def last_heartbeat(store) -> datetime | None:
    value = store.get_meta(HEARTBEAT)
    try:
        return datetime.fromisoformat(value) if value else None
    except ValueError:
        return None


def beat(store, at: datetime | None = None) -> None:
    store.set_meta(HEARTBEAT, (at or now()).isoformat(timespec="seconds"))


def save_report(store, report: dict) -> None:
    store.set_meta(REPORT, json.dumps(report))


def load_report(store, max_hours: int) -> dict | None:
    try:
        report = json.loads(store.get_meta(REPORT) or "null")
    except ValueError:
        return None
    if not report:
        return None
    at = datetime.fromisoformat(report["at"])
    return report if (now() - at).total_seconds() < max_hours * 3600 else None


class InstanceLock:
    """An exclusive lock on a file next to the database: a second `jobalerts run` on the same data
    stops instead of polling and pushing everything twice. Released when the process exits."""

    def __init__(self, path: Path):
        self.path = path
        self._fd: int | None = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        self._fd = fd
        return True

    def holder(self) -> str:
        try:
            return self.path.read_text().strip() or "?"
        except OSError:
            return "?"

    def release(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
