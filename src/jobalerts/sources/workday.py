"""Workday careers sites (the JSON 'cxs' API behind <tenant>.wdN.myworkdayjobs.com).

Big enterprise boards can hold thousands of jobs, so we read only the first few pages
(newest first) and rely on a `search` term in companies.yaml to narrow them. Workday only
says "Posted 3 Days Ago", so published_at is approximate until a match is enriched.
"""
from __future__ import annotations

import re
from datetime import timedelta

from ..models import Job, parse_dt, strip_html, utcnow
from .base import Source

PAGE = 20  # Workday rejects larger page sizes


def posted_on_to_dt(text: str):
    t = (text or "").lower()
    now = utcnow()
    if "today" in t:
        return now
    if "yesterday" in t:
        return now - timedelta(days=1)
    if m := re.search(r"(\d+)\+?\s+days?", t):
        return now - timedelta(days=int(m.group(1)))
    return None


class Workday(Source):
    name = "workday"

    def _base(self, board) -> str:
        p = board.params
        return f"https://{p['host']}/wday/cxs/{p['tenant']}/{p['site']}"

    async def fetch(self, board, http):
        jobs: list[Job] = []
        pages = max(1, int(board.params.get("pages") or 1))
        for page in range(pages):
            payload = {"appliedFacets": {}, "limit": PAGE, "offset": page * PAGE,
                       "searchText": board.params.get("search", "")}
            data = await http.post_json(f"{self._base(board)}/jobs", payload)
            postings = data.get("jobPostings", [])
            jobs.extend(self._job(board, p) for p in postings if p.get("externalPath"))
            if len(postings) < PAGE:
                break
        return jobs

    def _job(self, board, p) -> Job:
        p_host, site = board.params["host"], board.params["site"]
        location = p.get("locationsText", "")
        return Job(
            source=self.name,
            board=board.slug,
            job_id=p["externalPath"],
            company=board.name,
            title=p.get("title", "").strip(),
            url=f"https://{p_host}/{site}{p['externalPath']}",
            location=location,
            remote="remote" in location.lower() or "remote" in (p.get("remoteType") or "").lower(),
            published_at=posted_on_to_dt(p.get("postedOn", "")),
        )

    async def enrich(self, job, board, http):
        data = await http.get_json(f"{self._base(board)}{job.job_id}", conditional=False)
        info = data.get("jobPostingInfo") or {}
        job.description = strip_html(info.get("jobDescription", ""))
        if exact := parse_dt(info.get("startDate")):
            job.published_at = exact
        if "remote" in (info.get("remoteType") or "").lower():
            job.remote = True
