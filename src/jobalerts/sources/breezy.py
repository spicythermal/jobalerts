"""Breezy HR careers pages: https://<company>.breezy.hr/json"""
from __future__ import annotations

from ..models import Job, parse_dt
from .base import Source


class Breezy(Source):
    name = "breezy"

    async def fetch(self, board, http):
        data = await http.get_json(f"https://{board.slug}.breezy.hr/json")
        return [self._job(board, j) for j in data]

    def _job(self, board, j) -> Job:
        locations = j.get("locations") or [j.get("location") or {}]
        location = "; ".join(filter(None, (loc.get("name") for loc in locations)))
        return Job(
            source=self.name,
            board=board.slug,
            job_id=j["id"],
            company=board.name,
            title=(j.get("name") or "").strip(),
            url=j.get("url") or f"https://{board.slug}.breezy.hr/p/{j.get('friendly_id', j['id'])}",
            location=location,
            remote=any(loc.get("is_remote") for loc in locations),
            published_at=parse_dt(j.get("published_date")),
            salary=j.get("salary") or "",
            department=j.get("department") or "",
        )
