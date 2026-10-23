"""SmartRecruiters public Posting API: https://developers.smartrecruiters.com/docs/posting-api"""
from __future__ import annotations

import logging

from ..models import Job, parse_dt, strip_html
from .base import Source

log = logging.getLogger(__name__)

API = "https://api.smartrecruiters.com/v1/companies"
PAGE = 100
MAX_PAGES = 10


class SmartRecruiters(Source):
    name = "smartrecruiters"

    async def fetch(self, board, http):
        jobs: list[Job] = []
        params = {"limit": PAGE, "offset": 0}
        if board.params.get("search"):
            params["q"] = board.params["search"]
        # Read the whole (searched) board so a job shifting between pages never looks new.
        for _ in range(MAX_PAGES):
            data = await http.get_json(f"{API}/{board.slug}/postings", params=dict(params))
            content = data.get("content", [])
            jobs.extend(self._job(board, p) for p in content)
            params["offset"] += PAGE
            if not content or params["offset"] >= data.get("totalFound", 0):
                break
        else:
            log.warning("%s has %s postings; only the first %d are read. Narrow it with a `search` in "
                        "companies.yaml (words are OR-ed; quote a phrase for an exact match).",
                        board.key, data.get("totalFound"), PAGE * MAX_PAGES)
        return jobs

    def _job(self, board, p) -> Job:
        loc = p.get("location") or {}
        location = loc.get("fullLocation") or ", ".join(
            filter(None, [loc.get("city"), loc.get("region"), (loc.get("country") or "").upper()]))
        company = (p.get("company") or {}).get("name") or board.name
        return Job(
            source=self.name,
            board=board.slug,
            job_id=str(p["id"]),
            company=company,
            title=p.get("name", "").strip(),
            url=f"https://jobs.smartrecruiters.com/{board.slug}/{p['id']}",
            location=location,
            remote=bool(loc.get("remote")),
            published_at=parse_dt(p.get("releasedDate")),
            department=(p.get("department") or {}).get("label", ""),
        )

    async def enrich(self, job, board, http):
        data = await http.get_json(f"{API}/{board.slug}/postings/{job.job_id}", conditional=False)
        sections = ((data.get("jobAd") or {}).get("sections") or {})
        job.description = "\n\n".join(
            strip_html(s.get("text", "")) for s in sections.values() if isinstance(s, dict))
