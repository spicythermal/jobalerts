"""USAJobs search API (free key): https://developer.usajobs.gov/api-reference/get-api-search"""
from __future__ import annotations

from ..models import Job, format_salary, parse_dt
from ..vault import secret
from .base import Source, SourceConfigError

API = "https://data.usajobs.gov/api/search"


class USAJobs(Source):
    name = "usajobs"
    env_keys = ("USAJOBS_API_KEY", "USAJOBS_EMAIL")

    async def fetch(self, board, http):
        key, email = secret("USAJOBS_API_KEY"), secret("USAJOBS_EMAIL")
        if not key or not email:
            raise SourceConfigError("USAJOBS_API_KEY and USAJOBS_EMAIL must be set in .env or on the Settings page or on the Settings page")
        p = board.params
        params = {"Keyword": p.get("keyword", ""), "DatePosted": 2, "ResultsPerPage": 250,
                  "SortField": "opendate", "SortDirection": "desc"}
        if p.get("location"):
            params["LocationName"] = p["location"]
        if p.get("remote"):
            params["RemoteIndicator"] = "True"
        headers = {"User-Agent": email, "Authorization-Key": key}
        data = await http.get_json(API, params=params, headers=headers)
        items = (data.get("SearchResult") or {}).get("SearchResultItems") or []
        return [self._job(i) for i in items]

    def _job(self, item) -> Job:
        d = item.get("MatchedObjectDescriptor") or {}
        pay = (d.get("PositionRemuneration") or [{}])[0]
        details = (d.get("UserArea") or {}).get("Details") or {}
        location = d.get("PositionLocationDisplay", "")
        org = d.get("OrganizationName") or d.get("DepartmentName") or "US Government"
        return Job(
            source=self.name,
            board="",
            job_id=str(item.get("MatchedObjectId") or d.get("PositionID")),
            company=org,
            title=d.get("PositionTitle", "").strip(),
            url=d.get("PositionURI", ""),
            location=location,
            remote=bool(details.get("RemoteIndicator")) or "remote" in location.lower(),
            published_at=parse_dt(d.get("PublicationStartDate")),
            description="\n\n".join(filter(None, [details.get("JobSummary", ""),
                                                  d.get("QualificationSummary", "")])),
            salary=format_salary(pay.get("MinimumRange"), pay.get("MaximumRange"), "USD",
                                 {"PA": "yr", "PH": "hr"}.get(pay.get("RateIntervalCode", ""), "")),
            department=d.get("DepartmentName", ""),
        )
