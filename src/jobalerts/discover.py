"""Find which ATS a company uses, so it can be added to companies.yaml.

By name: try a few likely slugs against each platform's public API (a miss is a cheap 404).
By URL: recognise ATS links in the URL itself or in the careers page HTML.
"""
from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from pathlib import Path

import httpx

from .config import parse_workday_url
from .http import HttpClient, RateLimited
from .models import normalize_company

_EXTRA_SUFFIXES = {"technologies", "technology", "labs", "group", "holdings", "software", "systems", "ai", "hq"}

URL_PATTERNS = [
    ("greenhouse", re.compile(r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_board(?:/js)?\?for=)?([A-Za-z0-9_-]+)")),
    ("greenhouse", re.compile(r"boards-api\.greenhouse\.io/v1/boards/([A-Za-z0-9_-]+)")),
    ("lever", re.compile(r"jobs\.(?:eu\.)?lever\.co/([A-Za-z0-9_.-]+)")),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9_.%-]+)")),
    ("smartrecruiters", re.compile(r"(?:jobs|careers)\.smartrecruiters\.com/([A-Za-z0-9_-]+)")),
    ("workday", re.compile(r"https?://[a-z0-9-]+\.wd\d+\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?[A-Za-z0-9_-]+")),
]
_NOT_SLUGS = {"embed", "v1", "api", "js", "jobs", "careers"}


@dataclass
class Found:
    ats: str
    slug: str
    name: str
    jobs: int
    url: str = ""

    def entry(self, tier: str = "normal") -> dict:
        e = {"name": self.name, "ats": self.ats}
        if self.ats == "workday":
            e["url"] = self.url
        else:
            e["slug"] = self.slug
        e["tier"] = tier
        return e


def slug_candidates(name: str) -> list[str]:
    words = re.sub(r"[^a-z0-9 ]+", " ", name.lower()).split()
    core = normalize_company(name).split()
    trimmed = [w for w in core if w not in _EXTRA_SUFFIXES] or core
    candidates = ["".join(trimmed), "-".join(trimmed), "".join(core), "".join(words), trimmed[0] if trimmed else ""]
    seen: list[str] = []
    for c in candidates:
        if c and c not in seen:
            seen.append(c)
    return seen[:5]


async def probe(ats: str, slug: str, http: HttpClient, name: str = "", strict: bool = True) -> Found | None:
    """strict=True (name guessing) requires a non-empty board, to avoid false positives."""
    name = name or slug
    try:
        if ats == "greenhouse":
            data = await http.get_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
                                       conditional=False, allow_404=True)
            if isinstance(data, dict) and "jobs" in data:
                return Found(ats, slug, name, len(data["jobs"]))
        elif ats == "lever":
            data = await http.get_json(f"https://api.lever.co/v0/postings/{slug}", params={"mode": "json"},
                                       conditional=False, allow_404=True)
            if isinstance(data, list) and (data or not strict):
                return Found(ats, slug, name, len(data))
        elif ats == "ashby":
            data = await http.get_json(f"https://api.ashbyhq.com/posting-api/job-board/{slug}",
                                       conditional=False, allow_404=True)
            if isinstance(data, dict) and isinstance(data.get("jobs"), list) and (data["jobs"] or not strict):
                return Found(ats, slug, name, len(data["jobs"]))
        elif ats == "smartrecruiters":
            data = await http.get_json(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings",
                                       params={"limit": 1}, conditional=False, allow_404=True)
            # SmartRecruiters answers 200 for any company id, so an empty board means "not here".
            if isinstance(data, dict) and data.get("totalFound", 0) > 0:
                return Found(ats, slug, name, data["totalFound"])
    except (httpx.HTTPError, RateLimited, ValueError):
        return None
    return None


async def probe_workday(url: str, http: HttpClient, name: str = "") -> Found | None:
    try:
        host, tenant, site = parse_workday_url(url)
        data = await http.post_json(f"https://{host}/wday/cxs/{tenant}/{site}/jobs",
                                    {"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""}, allow_404=True)
    except (httpx.HTTPError, RateLimited, ValueError):
        return None
    if not isinstance(data, dict) or "jobPostings" not in data:
        return None
    return Found("workday", f"{tenant}/{site}", name or tenant, int(data.get("total") or 0), f"https://{host}/{site}")


async def find_by_name(name: str, http: HttpClient) -> list[Found]:
    candidates = slug_candidates(name)

    async def first_hit(ats: str) -> Found | None:
        for slug in candidates:  # sequential per platform; platforms run in parallel
            if found := await probe(ats, slug, http, name=name):
                return found
        return None

    results = await asyncio.gather(*(first_hit(a) for a in ("greenhouse", "lever", "ashby", "smartrecruiters")))
    return [r for r in results if r]


def links_in(text: str) -> list[tuple[str, str]]:
    """(ats, slug-or-url) pairs found in a URL or an HTML page."""
    hits: list[tuple[str, str]] = []
    for ats, rx in URL_PATTERNS:
        for m in rx.finditer(text):
            value = m.group(0) if ats == "workday" else m.group(1)
            if value.lower() in _NOT_SLUGS:
                continue
            if (ats, value) not in hits:
                hits.append((ats, value))
    return hits


async def find_by_url(url: str, http: HttpClient, name: str = "") -> list[Found]:
    hits = links_in(url)
    if not hits:
        hits = links_in(await http.get_text(url))
    found: list[Found] = []
    for ats, value in hits[:6]:
        f = (await probe_workday(value, http, name) if ats == "workday"
             else await probe(ats, value, http, name=name, strict=False))
        if f and all((f.ats, f.slug) != (x.ats, x.slug) for x in found):
            found.append(f)
    return found


def append_to_companies(path: Path, found: Found, tier: str) -> bool:
    """Append one entry (JSON flow style is valid YAML). Returns False if it's already listed."""
    text = path.read_text(encoding="utf-8") if path.exists() else "companies:\n"
    marker = found.url if found.ats == "workday" else f'"slug": "{found.slug}"'
    if marker in text or re.search(rf"slug:\s*['\"]?{re.escape(found.slug)}['\"]?\s*(,|}}|$)", text, re.M):
        return False
    if not text.endswith("\n"):
        text += "\n"
    text += f"  - {json.dumps(found.entry(tier))}\n"
    path.write_text(text, encoding="utf-8")
    return True
