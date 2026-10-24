"""Adapter interface: turn one board's API response into normalized Jobs."""
from __future__ import annotations

import os
from abc import ABC, abstractmethod
from typing import ClassVar

from ..config import Board
from ..http import HttpClient
from ..models import Job


class SourceConfigError(Exception):
    """The board can't be polled as configured (e.g. missing API key)."""


class Source(ABC):
    name: ClassVar[str]
    env_keys: ClassVar[tuple[str, ...]] = ()  # .env settings the source can't run without

    def missing_keys(self) -> list[str]:
        return [k for k in self.env_keys if not os.environ.get(k)]

    @abstractmethod
    async def fetch(self, board: Board, http: HttpClient) -> list[Job]:
        """List the board's current open jobs. Keep it to the cheapest call(s) the API offers."""

    async def enrich(self, job: Job, board: Board, http: HttpClient) -> None:
        """Fill job.description when the list call doesn't include it. Only called for keyword matches."""
        return None
