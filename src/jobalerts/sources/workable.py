"""Workable public jobs widget: https://apply.workable.com/api/v1/widget/accounts/<account>"""
from __future__ import annotations

from ..models import Job, parse_dt, strip_html
from .base import Source


class Workable(Source):
    name = "workable"

    async def fetch(self, board, http):
        data = await http.get_json(f"https://apply.workable.com/api/v1/widget/accounts/{board.slug}",
                                   params={"details": "true"})
        return [self._job(board, j) for j in data.get("jobs", [])]

    def _job(self, board, j) -> Job:
        places = [", ".join(filter(None, [loc.get("city"), loc.get("region"), loc.get("country")]))
                  for loc in j.get("locations") or []]
        if not any(places):
            places = [", ".join(filter(None, [j.get("city"), j.get("state"), j.get("country")]))]
        location = "; ".join(dict.fromkeys(filter(None, places)))
        remote = str(j.get("telecommuting")).lower() == "true"
        return Job(
            source=self.name,
            board=board.slug,
            job_id=j["shortcode"],
            company=board.name,
            title=(j.get("title") or "").strip(),
            url=j.get("url") or f"https://apply.workable.com/j/{j['shortcode']}",
            location=location or ("Remote" if remote else ""),
            remote=remote,
            published_at=parse_dt(j.get("published_on") or j.get("created_at")),
            description=strip_html(j.get("description") or ""),
            department=j.get("department") or "",
        )
