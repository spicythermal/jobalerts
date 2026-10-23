"""Polite async HTTP client: per-host pacing, conditional requests, Retry-After handling.

Every request to a host waits for that host's minimum interval, so many boards on one
platform (e.g. 100 Greenhouse companies) never burst. A 429/503 pushes the host's next
allowed time out by Retry-After and raises RateLimited; the scheduler backs the board off.
"""
from __future__ import annotations

import asyncio
import email.utils
import time
from collections import Counter, defaultdict
from typing import Any

import httpx

from . import __version__


class RateLimited(Exception):
    pass


class HttpClient:
    def __init__(
        self,
        contact_email: str = "",
        host_min_interval: dict[str, float] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        contact = f"; {contact_email}" if contact_email else ""
        self.user_agent = f"jobalerts/{__version__} (personal job alert script{contact})"
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(45.0, connect=10.0),  # some boards are multi-MB (Lever includes descriptions)
            headers={"User-Agent": self.user_agent, "Accept": "application/json"},
            follow_redirects=True,
            transport=transport,
        )
        intervals = dict(host_min_interval or {})
        self._default_interval = intervals.pop("default", 1.0)
        self._intervals = intervals
        self._next_ok: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._validators: dict[str, tuple[str | None, str | None, Any]] = {}
        self.stats: dict[str, Counter] = defaultdict(Counter)

    async def aclose(self) -> None:
        await self._client.aclose()

    def min_interval(self, host: str) -> float:
        """Longest configured suffix wins: 'myworkdayjobs.com' covers every Workday tenant host."""
        best, best_len = self._default_interval, -1
        for suffix, value in self._intervals.items():
            if (host == suffix or host.endswith("." + suffix)) and len(suffix) > best_len:
                best, best_len = value, len(suffix)
        return best

    async def _wait_turn(self, host: str) -> None:
        async with self._locks[host]:
            wait = self._next_ok.get(host, 0.0) - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._next_ok[host] = time.monotonic() + self.min_interval(host)

    def _back_off_host(self, host: str, response: httpx.Response) -> float:
        delay = _retry_after_seconds(response.headers.get("Retry-After")) or 120.0
        delay = min(delay, 3600.0)
        self._next_ok[host] = max(self._next_ok.get(host, 0.0), time.monotonic() + delay)
        return delay

    async def _send(self, method: str, url: str, **kwargs) -> httpx.Response:
        host = httpx.URL(url).host
        await self._wait_turn(host)
        self.stats[host]["requests"] += 1
        try:
            response = await self._client.request(method, url, **kwargs)
        except httpx.HTTPError:
            self.stats[host]["errors"] += 1
            raise
        if response.status_code in (429, 503):
            self.stats[host]["rate_limited"] += 1
            delay = self._back_off_host(host, response)
            raise RateLimited(f"{host} returned {response.status_code}; backing off {delay:.0f}s")
        if response.status_code >= 400 and response.status_code != 404:
            self.stats[host]["errors"] += 1
        return response

    async def get_json(
        self, url: str, *, params: dict | None = None, headers: dict | None = None,
        conditional: bool = True, allow_404: bool = False,
    ) -> Any:
        """GET JSON. With conditional=True, replays ETag/Last-Modified and serves 304s from memory."""
        cache_key = str(httpx.URL(url, params=params)) if conditional else ""
        hdrs = dict(headers or {})
        cached = self._validators.get(cache_key) if conditional else None
        if cached:
            etag, last_modified, _ = cached
            if etag:
                hdrs["If-None-Match"] = etag
            if last_modified:
                hdrs["If-Modified-Since"] = last_modified
        response = await self._send("GET", url, params=params, headers=hdrs)
        if response.status_code == 304 and cached:
            self.stats[response.url.host]["not_modified"] += 1
            return cached[2]
        if allow_404 and response.status_code == 404:
            return None
        response.raise_for_status()
        data = response.json()
        etag, last_modified = response.headers.get("ETag"), response.headers.get("Last-Modified")
        if conditional and (etag or last_modified):
            self._validators[cache_key] = (etag, last_modified, data)
        return data

    async def post_json(self, url: str, payload: dict, *, headers: dict | None = None,
                        allow_404: bool = False) -> Any:
        response = await self._send("POST", url, json=payload, headers=headers)
        if allow_404 and response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()

    async def get_text(self, url: str) -> str:
        response = await self._send("GET", url, headers={"Accept": "text/html,*/*"})
        response.raise_for_status()
        return response.text

    def drain_stats(self) -> dict[str, Counter]:
        out, self.stats = self.stats, defaultdict(Counter)
        return out


def _retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, when.timestamp() - time.time())
