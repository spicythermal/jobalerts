"""Greenhouse public job board API: https://developers.greenhouse.io/job-board.html"""
from __future__ import annotations

from ..models import Job, format_salary, parse_dt, strip_html
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
        # pay_transparency adds the posted pay ranges (often one per US pay zone); asked for here rather
        # than on the list, where it can make the download several times larger.
        data = await http.get_json(f"{API}/{board.slug}/jobs/{job.job_id}?pay_transparency=true", conditional=False)
        job.description = strip_html(data.get("content", ""))
        ranges = [r for r in data.get("pay_input_ranges") or [] if r.get("max_cents")]
        if ranges and not job.salary:
            # Several zones: lowest to highest, like a multi-level range in a description.
            job.salary = format_salary(min(r.get("min_cents") or r["max_cents"] for r in ranges) / 100,
                                       max(r["max_cents"] for r in ranges) / 100,
                                       ranges[0].get("currency_type") or "USD", "yr")
