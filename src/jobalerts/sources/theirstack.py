"""TheirStack job search API: https://theirstack.com/en/docs/api-reference/jobs/search_jobs_v1

Every job returned costs one API credit (free plan: 200 a month, at most 25 per call), and asking
for the same job twice costs twice. So each poll only asks for jobs TheirStack found since the
last successful poll of that search (`discovered_at_gte`), and `limit` caps one poll's cost.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import httpx

from ..models import Job, format_salary, parse_dt, strip_html
from .base import Source, SourceConfigError

URL = "https://api.theirstack.com/v1/jobs/search"
PERIOD = {"year": "yr", "hour": "hour", "day": "day", "week": "week", "month": "month"}


class TheirStack(Source):
    name = "theirstack"
    env_keys = ("THEIRSTACK_API_KEY",)

    def __init__(self) -> None:
        self._since: dict[str, datetime] = {}  # board key -> start of the last successful poll

    def payload(self, params: dict, since: datetime) -> dict:
        body = {
            "job_title_or": list(params.get("titles", [])),
            "posted_at_max_age_days": params.get("max_age_days", 7),  # the API needs a posted_at filter
            "discovered_at_gte": since.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "company_type": "direct_employer",                          # recruiters' reposts cost credits too
            "limit": min(int(params.get("limit", 10)), 25),             # free plan: 25 per call
            "page": 0,
        }
        for ours, theirs in (("countries", "job_country_code_or"), ("exclude_titles", "job_title_not"),
                             ("description", "job_description_contains_or")):
            if params.get(ours):
                body[theirs] = list(params[ours])
        return body

    async def fetch(self, board, http):
        key = os.environ.get("THEIRSTACK_API_KEY")
        if not key:
            raise SourceConfigError("THEIRSTACK_API_KEY must be set in .env")
        started = datetime.now(timezone.utc)
        # First poll after a start: look back one interval, so a restart doesn't re-buy the last week.
        since = self._since.get(board.key, started - timedelta(seconds=board.interval))
        headers = {"Authorization": f"Bearer {key}"}
        try:
            data = await http.post_json(URL, self.payload(board.params, since), headers=headers)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 402:
                raise RuntimeError("TheirStack is out of API credits (402); the free plan refills monthly") from e
            raise
        self._since[board.key] = started
        return [self._job(d) for d in data.get("data", [])]

    def _job(self, d) -> Job:
        company = (d.get("company_object") or {}).get("name") or d.get("company") or ""
        remote = bool(d.get("remote"))
        location = d.get("long_location") or d.get("location") or ("Remote" if remote else "")
        return Job(
            source=self.name,
            board="",
            job_id=str(d["id"]),
            company=company,
            title=(d.get("job_title") or "").strip(),
            url=d.get("final_url") or d.get("url") or d.get("source_url") or "",  # final_url: the employer's page
            location=location,
            remote=remote,
            published_at=parse_dt(d.get("date_posted")),
            description=strip_html(d.get("description") or ""),
            salary=format_salary(d.get("min_salary"), d.get("max_salary"), d.get("salary_currency") or "USD",
                                 PERIOD.get(d.get("salary_period") or "", "")) or (d.get("salary_string") or ""),
        )
