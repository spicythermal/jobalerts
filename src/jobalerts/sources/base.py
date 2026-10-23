"""Adapter interface: turn one board's API response into normalized Jobs."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar

from ..config import Board
from ..http import HttpClient
from ..models import Job


class SourceConfigError(Exception):
    """The board can't be polled as configured (e.g. missing API key)."""


class Source(ABC):
    name: ClassVar[str]

    @abstractmethod
    async def fetch(self, board: Board, http: HttpClient) -> list[Job]:
        """List the board's current open jobs. Keep it to the cheapest call(s) the API offers."""

    async def enrich(self, job: Job, board: Board, http: HttpClient) -> None:
        """Fill job.description when the list call doesn't include it. Only called for keyword matches."""
        return None
