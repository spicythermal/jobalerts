"""UKG Pro (UltiPro) Recruiting job boards: recruiting[N].ultipro.com/<tenant>/JobBoard/<board id>.

The board page loads its list from a JSON POST, which returns the newest postings first.
Only a one-paragraph summary comes with the list; the full ad is an HTML page.
"""
from __future__ import annotations

from ..models import Job, parse_dt
from .base import Source

PAGE = 50


class UltiPro(Source):
    name = "ultipro"

    def _base(self, board) -> str:
        p = board.params
        return f"https://{p['host']}/{p['tenant']}/JobBoard/{p['board_id']}"

    async def fetch(self, board, http):
        jobs: list[Job] = []
        pages = max(1, int(board.params.get("pages") or 1))
        for page in range(pages):
            payload = {"opportunitySearch": {
                "Top": PAGE, "Skip": page * PAGE, "QueryString": board.params.get("search") or "",
                "OrderBy": [{"Value": "postedDateDesc", "PropertyName": "PostedDate", "Ascending": False}],
                "Filters": []}}
            data = await http.post_json(f"{self._base(board)}/JobBoardView/LoadSearchResults", payload)
            found = data.get("opportunities") or []
            jobs.extend(self._job(board, o) for o in found)
            if len(found) < PAGE or len(jobs) >= int(data.get("totalCount") or 0):
                break
        return jobs

    def _job(self, board, o) -> Job:
        places = []
        for loc in o.get("Locations") or []:
            a = loc.get("Address") or {}
            parts = [a.get("City"), (a.get("State") or {}).get("Name"), (a.get("Country") or {}).get("Name")]
            places.append(", ".join(filter(None, parts)) or loc.get("LocalizedDescription") or "")
        return Job(
            source=self.name,
            board=board.slug,
            job_id=o["Id"],
            company=board.name,
            title=(o.get("Title") or "").strip(),
            url=f"{self._base(board)}/OpportunityDetail?opportunityId={o['Id']}",
            location="; ".join(filter(None, places)),
            remote=o.get("JobLocationType") == 3,  # 1 on-site, 2 hybrid, 3 remote
            published_at=parse_dt(o.get("PostedDate")),
            description=o.get("BriefDescription") or "",
            department=o.get("JobCategoryName") or "",
        )
