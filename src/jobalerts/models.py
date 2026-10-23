"""Normalized job record shared by every source adapter, plus text/date helpers."""
from __future__ import annotations

import hashlib
import html
import re
from dataclasses import dataclass
from datetime import datetime, timezone

_COMPANY_SUFFIXES = {
    "inc", "incorporated", "llc", "ltd", "limited", "corp", "corporation", "co", "company",
    "plc", "gmbh", "the", "lp", "llp",
}


@dataclass
class Job:
    source: str          # adapter name: greenhouse, lever, usajobs, ...
    board: str           # board slug for ATS sources, "" for aggregators
    job_id: str
    company: str
    title: str
    url: str
    location: str = ""
    remote: bool = False
    published_at: datetime | None = None
    description: str = ""
    salary: str = ""
    department: str = ""

    @property
    def key(self) -> str:
        return f"{self.source}:{self.board}:{self.job_id}"

    @property
    def fingerprint(self) -> str:
        """Company + title, used to spot the same posting arriving from a different source."""
        raw = f"{normalize_company(self.company)}|{_norm(self.title)}"
        return hashlib.sha1(raw.encode()).hexdigest()[:16]

    def age_days(self, now: datetime | None = None) -> float | None:
        if self.published_at is None:
            return None
        return ((now or utcnow()) - self.published_at).total_seconds() / 86400


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def normalize_company(name: str) -> str:
    words = [w for w in _norm(name).split() if w not in _COMPANY_SUFFIXES]
    return " ".join(words)


def parse_dt(value) -> datetime | None:
    """ISO-8601 string or epoch milliseconds -> aware UTC datetime (None if unparseable)."""
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
        text = str(value).strip()
        # Some APIs send more than 6 fractional digits, which fromisoformat rejects.
        text = re.sub(r"(\.\d{6})\d+", r"\1", text)
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (ValueError, OverflowError, OSError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def strip_html(text: str) -> str:
    """HTML (possibly entity-escaped, as Greenhouse sends it) -> readable plain text."""
    if not text:
        return ""
    text = html.unescape(text)
    text = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", text)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</h\d>", "\n", text)
    text = re.sub(r"(?i)<li[^>]*>", "\n- ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\s*\n\s*", "\n", text)
    return text.strip()


def format_salary(low, high, currency: str = "", interval: str = "") -> str:
    def fmt(v) -> str:
        try:
            v = float(v)
        except (TypeError, ValueError):
            return ""
        return f"{v / 1000:.0f}K" if v >= 1000 else f"{v:g}"

    lo, hi = fmt(low), fmt(high)
    if not lo and not hi:
        return ""
    amount = f"{lo}-{hi}" if lo and hi and lo != hi else (lo or hi)
    prefix = "$" if currency.upper() in ("", "USD") else f"{currency.upper()} "
    suffix = f"/{interval.lower()}" if interval else ""
    return f"{prefix}{amount}{suffix}"
