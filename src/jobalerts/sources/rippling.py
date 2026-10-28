"""Rippling ATS job boards: https://ats.rippling.com/<board>/jobs (JSON at /api/v2/board/<board>/jobs)."""
from __future__ import annotations

from ..models import Job, parse_dt, strip_html
from .base import Source

API = "https://ats.rippling.com/api/v2/board"
PAGE = 50
MAX_PAGES = 10


class Rippling(Source):
    name = "rippling"

    async def fetch(self, board, http):
        jobs: list[Job] = []
        for page in range(MAX_PAGES):
            data = await http.get_json(f"{API}/{board.slug}/jobs", params={"page": page, "pageSize": PAGE})
            jobs.extend(self._job(board, j) for j in data.get("items", []))
            if page + 1 >= int(data.get("totalPages") or 1):
                break
        return jobs

    def _job(self, board, j) -> Job:
        locations = j.get("locations") or []
        location = "; ".join(filter(None, (loc.get("name") for loc in locations)))
        return Job(
            source=self.name,
            board=board.slug,
            job_id=j["id"],
            company=board.name,
            title=(j.get("name") or "").strip(),
            url=j.get("url") or f"https://ats.rippling.com/{board.slug}/jobs/{j['id']}",
            location=location,
            remote=any(loc.get("workplaceType") == "REMOTE" for loc in locations),
            published_at=None,  # only the detail call has createdOn; first_seen_at stands in
            department=(j.get("department") or {}).get("name") or "",
        )

    async def enrich(self, job, board, http):
        data = await http.get_json(f"{API}/{board.slug}/jobs/{job.job_id}", conditional=False)
        desc = data.get("description") or {}
        parts = desc.values() if isinstance(desc, dict) else [desc]
        job.description = "\n\n".join(strip_html(p) for p in parts if isinstance(p, str))
        job.published_at = job.published_at or parse_dt(data.get("createdOn"))
