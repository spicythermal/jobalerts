"""SAP SuccessFactors career sites (Career Site Builder, e.g. careers.bwxt.com).

Each site publishes an RSS feed of its newest 20 postings, full ad included:
<site>/services/rss/job/?locale=en_US&keywords=<search>. The location is only in the
title, as "Quality Engineer (Jonesborough, TN, US)".
"""
from __future__ import annotations

import email.utils
import re
import xml.etree.ElementTree as ET
from datetime import timezone

from ..models import Job, strip_html
from .base import Source

_ID = re.compile(r"/(\d{5,})/?(?:\?|$)")
_TITLE = re.compile(r"^(.*?)\s*\(([^()]*)\)\s*$")


def _pub_date(text: str):
    try:
        return email.utils.parsedate_to_datetime(text).astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


class SuccessFactors(Source):
    name = "successfactors"

    async def fetch(self, board, http):
        url = board.params["url"].rstrip("/")
        search = board.params.get("search") or ""
        text = await http.get_text(f"{url}/services/rss/job/?locale=en_US&keywords={search}")
        return [self._job(board, item) for item in ET.fromstring(text).iter("item")
                if _ID.search(item.findtext("link") or "")]

    def _job(self, board, item) -> Job:
        link = (item.findtext("link") or "").split("?")[0]
        title = (item.findtext("title") or "").strip()
        location = ""
        if m := _TITLE.match(title):
            title, location = m.group(1), m.group(2)
        return Job(
            source=self.name,
            board=board.slug,
            job_id=_ID.search(link + "?").group(1),
            company=board.name,
            title=title,
            url=link,
            location=location,
            remote="remote" in location.lower() or "remote" in title.lower(),
            published_at=_pub_date(item.findtext("pubDate") or ""),
            description=strip_html(item.findtext("description") or ""),
        )
