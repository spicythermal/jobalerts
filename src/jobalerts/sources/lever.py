"""Lever postings API: https://github.com/lever/postings-api"""
from __future__ import annotations

from ..models import Job, format_salary, parse_dt
from .base import Source


class Lever(Source):
    name = "lever"

    async def fetch(self, board, http):
        host = "api.eu.lever.co" if board.params.get("region") == "eu" else "api.lever.co"
        data = await http.get_json(f"https://{host}/v0/postings/{board.slug}", params={"mode": "json"})
        return [self._job(board, p) for p in data]

    def _job(self, board, p) -> Job:
        cats = p.get("categories") or {}
        location = cats.get("location") or ", ".join(cats.get("allLocations") or [])
        salary = p.get("salaryRange") or {}
        description = "\n\n".join(filter(None, [
            p.get("descriptionPlain", ""),
            *(f"{lst.get('text', '')}\n{lst.get('content', '')}" for lst in p.get("lists") or []),
            p.get("additionalPlain", ""),
        ]))
        return Job(
            source=self.name,
            board=board.slug,
            job_id=p["id"],
            company=board.name,
            title=p.get("text", "").strip(),
            url=p.get("hostedUrl", ""),
            location=location,
            remote=p.get("workplaceType") == "remote" or "remote" in location.lower(),
            published_at=parse_dt(p.get("createdAt")),
            description=description,
            salary=format_salary(salary.get("min"), salary.get("max"), salary.get("currency", ""),
                                 salary.get("interval", "").replace("-", " ")),
            department=cats.get("team") or cats.get("department") or "",
        )
