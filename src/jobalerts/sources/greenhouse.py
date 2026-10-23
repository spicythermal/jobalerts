"""Greenhouse public job board API: https://developers.greenhouse.io/job-board.html"""
from __future__ import annotations

from ..models import Job, parse_dt, strip_html
from .base import Source

API = "https://boards-api.greenhouse.io/v1/boards"


class Greenhouse(Source):
    name = "greenhouse"

    async def fetch(self, board, http):
        # The list without ?content=true is a fraction of the size; descriptions are fetched per match.
        data = await http.get_json(f"{API}/{board.slug}/jobs")
        return [self._job(board, j) for j in data.get("jobs", [])]

    def _job(self, board, j) -> Job:
        location = (j.get("location") or {}).get("name", "")
        departments = j.get("departments") or []
        return Job(
            source=self.name,
            board=board.slug,
            job_id=str(j["id"]),
            company=j.get("company_name") or board.name,
            title=j.get("title", "").strip(),
            url=j.get("absolute_url", ""),
            location=location,
            remote="remote" in location.lower(),
            published_at=parse_dt(j.get("first_published") or j.get("updated_at")),
            department=departments[0].get("name", "") if departments else "",
        )

    async def enrich(self, job, board, http):
        data = await http.get_json(f"{API}/{board.slug}/jobs/{job.job_id}", conditional=False)
        job.description = strip_html(data.get("content", ""))
