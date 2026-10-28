"""BambooHR careers page JSON: https://<company>.bamboohr.com/careers/list (and /careers/<id>/detail)."""
from __future__ import annotations

from ..models import Job, parse_dt, strip_html
from .base import Source

ACCEPT = {"Accept": "application/json"}


class BambooHR(Source):
    name = "bamboohr"

    async def fetch(self, board, http):
        data = await http.get_json(f"https://{board.slug}.bamboohr.com/careers/list", headers=ACCEPT)
        return [self._job(board, j) for j in data.get("result", [])]

    def _job(self, board, j) -> Job:
        loc = j.get("location") or {}
        location = ", ".join(filter(None, [loc.get("city"), loc.get("state")]))
        remote = bool(j.get("isRemote")) or str(j.get("locationType")) == "1"  # 0 on-site, 1 remote, 2 hybrid
        return Job(
            source=self.name,
            board=board.slug,
            job_id=str(j["id"]),
            company=board.name,
            title=(j.get("jobOpeningName") or "").strip(),
            url=f"https://{board.slug}.bamboohr.com/careers/{j['id']}",
            location=location or ("Remote" if remote else ""),
            remote=remote,
            published_at=None,  # only the detail call has datePosted; first_seen_at stands in
            department=j.get("departmentLabel") or "",
        )

    async def enrich(self, job, board, http):
        data = await http.get_json(f"https://{board.slug}.bamboohr.com/careers/{job.job_id}/detail",
                                   headers=ACCEPT, conditional=False)
        opening = (data.get("result") or {}).get("jobOpening") or {}
        job.description = strip_html(opening.get("description") or "")
        job.published_at = job.published_at or parse_dt(opening.get("datePosted"))
