"""Adzuna job search API (free key, daily call cap): https://developer.adzuna.com/docs/search"""
from __future__ import annotations

import os

from ..models import Job, format_salary, parse_dt, strip_html
from .base import Source, SourceConfigError


class Adzuna(Source):
    name = "adzuna"

    async def fetch(self, board, http):
        app_id, app_key = os.environ.get("ADZUNA_APP_ID"), os.environ.get("ADZUNA_APP_KEY")
        if not app_id or not app_key:
            raise SourceConfigError("ADZUNA_APP_ID and ADZUNA_APP_KEY must be set in .env")
        p = board.params
        params = {"app_id": app_id, "app_key": app_key, "what": p.get("what", ""),
                  "max_days_old": 2, "sort_by": "date", "results_per_page": 50}
        if p.get("where"):
            params["where"] = p["where"]
        country = p.get("country", "us")
        # Adzuna has no ETag support and the key is in the URL; skip conditional caching.
        data = await http.get_json(f"https://api.adzuna.com/v1/api/jobs/{country}/search/1",
                                   params=params, conditional=False)
        return [self._job(r) for r in data.get("results", [])]

    def _job(self, r) -> Job:
        location = (r.get("location") or {}).get("display_name", "")
        return Job(
            source=self.name,
            board="",
            job_id=str(r["id"]),
            company=(r.get("company") or {}).get("display_name", ""),
            title=strip_html(r.get("title", "")),
            url=r.get("redirect_url", ""),
            location=location,
            remote="remote" in location.lower() or "remote" in r.get("title", "").lower(),
            published_at=parse_dt(r.get("created")),
            description=strip_html(r.get("description", "")),
            salary=format_salary(r.get("salary_min"), r.get("salary_max"), "USD", "yr"),
        )
