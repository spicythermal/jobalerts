"""Adzuna job search API (free key, daily call cap): https://developer.adzuna.com/docs/search"""
from __future__ import annotations

from ..models import Job, format_salary, parse_dt, strip_html
from ..vault import secret
from .base import Source, SourceConfigError

CURRENCY = {"gb": "GBP", "at": "EUR", "be": "EUR", "de": "EUR", "es": "EUR", "fr": "EUR", "it": "EUR",
            "nl": "EUR", "ch": "CHF", "pl": "PLN", "in": "INR", "sg": "SGD", "br": "BRL", "mx": "MXN",
            "ca": "CAD", "au": "AUD", "nz": "NZD", "za": "ZAR"}


class Adzuna(Source):
    name = "adzuna"
    env_keys = ("ADZUNA_APP_ID", "ADZUNA_APP_KEY")

    async def fetch(self, board, http):
        app_id, app_key = secret("ADZUNA_APP_ID"), secret("ADZUNA_APP_KEY")
        if not app_id or not app_key:
            raise SourceConfigError("ADZUNA_APP_ID and ADZUNA_APP_KEY must be set in .env or on the Settings page or on the Settings page")
        p = board.params
        params = {"app_id": app_id, "app_key": app_key, "what": p.get("what", ""),
                  "max_days_old": 2, "sort_by": "date", "results_per_page": 50}
        for key in ("what_or", "what_phrase", "where"):  # what_or: any of these words
            if p.get(key):
                params[key] = p[key]
        country = p.get("country", "us")
        # Adzuna has no ETag support and the key is in the URL; skip conditional caching.
        data = await http.get_json(f"https://api.adzuna.com/v1/api/jobs/{country}/search/1",
                                   params=params, conditional=False)
        return [self._job(r, CURRENCY.get(country, "USD")) for r in data.get("results", [])]

    def _job(self, r, currency: str = "USD") -> Job:
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
            salary=format_salary(r.get("salary_min"), r.get("salary_max"), currency, "yr"),
        )
