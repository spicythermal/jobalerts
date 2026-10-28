"""Personio public job feed (XML): https://<company>.jobs.personio.com/xml"""
from __future__ import annotations

import xml.etree.ElementTree as ET

from ..models import Job, parse_dt, strip_html
from .base import Source


class Personio(Source):
    name = "personio"

    async def fetch(self, board, http):
        text = await http.get_text(f"https://{board.slug}.jobs.personio.com/xml?language=en")
        return [self._job(board, p) for p in ET.fromstring(text).iter("position")]

    def _job(self, board, p) -> Job:
        def get(tag: str) -> str:
            return (p.findtext(tag) or "").strip()

        offices = [get("office")] + [(o.text or "").strip() for o in p.iter("additionalOffice")]
        location = "; ".join(dict.fromkeys(filter(None, offices)))
        description = "\n\n".join(
            f"{d.findtext('name', '').strip()}\n{strip_html(d.findtext('value', ''))}"
            for d in p.iter("jobDescription"))
        return Job(
            source=self.name,
            board=board.slug,
            job_id=get("id"),
            company=board.name,
            title=get("name"),
            url=f"https://{board.slug}.jobs.personio.com/job/{get('id')}",
            location=location,
            remote="remote" in location.lower(),
            published_at=parse_dt(get("createdAt")),
            description=description,
            department=get("department"),
        )
