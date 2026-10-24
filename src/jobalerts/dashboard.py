"""Top recent matches, for `jobalerts top` and a Home Assistant sensor (POST /api/states/<entity_id>)."""
from __future__ import annotations

import logging
from datetime import timedelta

import httpx

from .config import HomeAssistant
from .filters import JobFilter
from .models import Job, parse_dt, utcnow
from .store import Store

log = logging.getLogger(__name__)


def _age(published) -> str:
    if published is None:
        return ""
    hours = (utcnow() - published).total_seconds() / 3600
    return f"{hours:.0f}h ago" if hours < 48 else f"{hours / 24:.0f}d ago"


def top_matches(store: Store, job_filter: JobFilter, days: int, n: int) -> tuple[int, list[dict]]:
    """(number of matches posted in the last `days`, the best `n` of them).

    Ranked by AI score, then newest first. Jobs that were already open when a board was first
    polled (baseline) count too if they pass the title/location filter, so the list isn't empty
    right after a deploy."""
    matches = []
    for r in store.recent_candidates(utcnow() - timedelta(days=days)):
        published = parse_dt(r["published_at"])
        if r["status"] == "baseline":
            job = Job(r["source"], r["board"], r["job_id"], r["company"] or "", r["title"] or "", r["url"] or "",
                      location=r["location"] or "", remote=bool(r["remote"]), published_at=published)
            if not job_filter.check_basic(job)[0]:
                continue
        matches.append({
            "company": r["company"] or "",
            "title": r["title"] or "",
            "location": r["location"] or "",
            "salary": r["salary"] or "",
            "url": r["url"] or "",
            "score": r["score"],
            "summary": r["score_summary"] or "",
            "best_resume": r["best_resume"] or "",
            "posted": r["published_at"] or r["first_seen_at"],
            "age": _age(published or parse_dt(r["first_seen_at"])),
            "pushed": r["status"] == "alerted",
        })
    matches.sort(key=lambda m: m["posted"], reverse=True)
    matches.sort(key=lambda m: -1 if m["score"] is None else m["score"], reverse=True)
    return len(matches), matches[:n]


class HomeAssistantPublisher:
    def __init__(self, cfg: HomeAssistant, transport: httpx.AsyncBaseTransport | None = None):
        self.cfg = cfg
        self._client = httpx.AsyncClient(timeout=15.0, transport=transport)

    @property
    def configured(self) -> bool:
        return bool(self.cfg.url and self.cfg.token)

    async def aclose(self) -> None:
        await self._client.aclose()

    def payload(self, total: int, jobs: list[dict]) -> dict:
        top = f"{jobs[0]['company']}: {jobs[0]['title']}" if jobs else "none"
        return {
            "state": total,  # number of matches, so HA can graph it; the details are attributes
            "attributes": {
                "friendly_name": "Job matches",
                "icon": "mdi:briefcase-search",
                "unit_of_measurement": "jobs",
                "days": self.cfg.days,
                "top": top[:250],
                "jobs": jobs,
                "updated": utcnow().isoformat(timespec="seconds"),
            },
        }

    async def publish(self, total: int, jobs: list[dict]) -> None:
        url = f"{self.cfg.url.rstrip('/')}/api/states/{self.cfg.entity_id}"
        r = await self._client.post(url, json=self.payload(total, jobs),
                                    headers={"Authorization": f"Bearer {self.cfg.token}"})
        r.raise_for_status()
        log.debug("Home Assistant %s updated: %d matches", self.cfg.entity_id, total)
