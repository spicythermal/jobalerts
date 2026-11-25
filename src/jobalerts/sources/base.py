"""Adapter interface: turn one board's API response into normalized Jobs."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar

from ..config import Board
from ..http import HttpClient
from ..models import Job
from ..vault import secret


class SourceConfigError(Exception):
    """The board can't be polled as configured (e.g. missing API key)."""


class Source(ABC):
    name: ClassVar[str]
    env_keys: ClassVar[tuple[str, ...]] = ()  # keys (.env or Settings page) the source can't run without

    def missing_keys(self) -> list[str]:
        return [k for k in self.env_keys if not secret(k)]

    @abstractmethod
    async def fetch(self, board: Board, http: HttpClient) -> list[Job]:
        """List the board's current open jobs. Keep it to the cheapest call(s) the API offers."""

    def resume(self, board: Board, last_ok) -> None:
        """Before a catch-up poll (after the computer was off): widen the next search to reach back to
        `last_ok` (a datetime), for searches that only look at a recent window. Boards list all their
        open jobs anyway, so the default does nothing."""
        return None

    async def enrich(self, job: Job, board: Board, http: HttpClient) -> None:
        """Fill job.description when the list call doesn't include it. Only called for keyword matches."""
        return None


def lookback_days(last_ok, floor: int, cap: int) -> int:
    """Whole days back to `last_ok` (plus one for safety), at least `floor`, at most `cap`."""
    from datetime import datetime, timezone
    if last_ok is None:
        return floor
    days = (datetime.now(timezone.utc) - last_ok).total_seconds() / 86400
    return max(floor, min(cap, int(days) + 2))
