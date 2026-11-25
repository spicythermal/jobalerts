"""JSearch on RapidAPI (paid aggregator slot, off by default): https://rapidapi.com/letscrape-6bRBa3QguO5/api/jsearch

Free plan is ~200 requests/month, so keep `interval` long (14400s = 6/day per search).
"""
from __future__ import annotations

from ..models import Job, format_salary, parse_dt
from ..vault import secret
from .base import Source, SourceConfigError

HOST = "jsearch.p.rapidapi.com"


class JSearch(Source):
    name = "jsearch"
    env_keys = ("RAPIDAPI_KEY",)

    async def fetch(self, board, http):
        key = secret("RAPIDAPI_KEY")
        if not key:
            raise SourceConfigError("RAPIDAPI_KEY must be set in .env or on the Settings page or on the Settings page")
        params = {"query": board.params.get("query", ""), "page": 1, "num_pages": 1, "date_posted": "today"}
        headers = {"X-RapidAPI-Key": key, "X-RapidAPI-Host": HOST}
        data = await http.get_json(f"https://{HOST}/search", params=params, headers=headers, conditional=False)
        return [self._job(d) for d in data.get("data", [])]

    def _job(self, d) -> Job:
        location = ", ".join(filter(None, [d.get("job_city"), d.get("job_state"), d.get("job_country")]))
        return Job(
            source=self.name,
            board="",
            job_id=str(d["job_id"]),
            company=d.get("employer_name", ""),
            title=d.get("job_title", "").strip(),
            url=d.get("job_apply_link") or d.get("job_google_link", ""),
            location=location or ("Remote" if d.get("job_is_remote") else ""),
            remote=bool(d.get("job_is_remote")),
            published_at=parse_dt(d.get("job_posted_at_datetime_utc")),
            description=d.get("job_description", ""),
            salary=format_salary(d.get("job_min_salary"), d.get("job_max_salary"),
                                 d.get("job_salary_currency") or "USD", d.get("job_salary_period") or ""),
        )
