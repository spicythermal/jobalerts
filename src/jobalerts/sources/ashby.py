"""Ashby public job posting API: https://developers.ashbyhq.com/docs/public-job-posting-api"""
from __future__ import annotations

from ..models import Job, parse_dt
from .base import Source

API = "https://api.ashbyhq.com/posting-api/job-board"


class Ashby(Source):
    name = "ashby"

    async def fetch(self, board, http):
        data = await http.get_json(f"{API}/{board.slug}", params={"includeCompensation": "true"})
        return [self._job(board, j) for j in data.get("jobs", []) if j.get("isListed", True)]

    def _job(self, board, j) -> Job:
        locations = [j.get("location") or ""] + [
            s.get("location", "") for s in j.get("secondaryLocations") or []]
        location = "; ".join(filter(None, locations))
        comp = j.get("compensation") or {}
        return Job(
            source=self.name,
            board=board.slug,
            job_id=j["id"],
            company=board.name,
            title=j.get("title", "").strip(),
            url=j.get("jobUrl", ""),
            location=location,
            remote=bool(j.get("isRemote")) or j.get("workplaceType") == "Remote",
            published_at=parse_dt(j.get("publishedAt")),
            description=j.get("descriptionPlain", ""),
            salary=comp.get("scrapeableCompensationSalarySummary") or comp.get("compensationTierSummary") or "",
            department=j.get("department") or j.get("team") or "",
        )
