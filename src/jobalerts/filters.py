"""Cheap keyword prefilter. Runs before any AI scoring so only plausible jobs cost money.

Phrases match whole words, case-insensitive, with plural endings ("beam" matches "Beams").
A * at either end lets the phrase run into a longer word: "*physicist" matches "Cryophysicist",
"research*" matches "Researcher".
"""
from __future__ import annotations

import re

from .config import Criteria
from .models import Job


def _normalize(text: str) -> str:
    return " ".join(text.lower().replace("\u2019", "'").split())


def phrase_pattern(phrase: str) -> re.Pattern:
    p = _normalize(phrase)
    core = p.strip("*").strip()
    # \w-based boundaries still work for phrases like "c++" or ".net", and for accented words.
    start = r"\w*" if p.startswith("*") else r"(?<!\w)"
    end = r"\w*" if p.endswith("*") else r"(?:s|es)?"
    return re.compile(rf"{start}{re.escape(core)}{end}(?!\w)")


def _compile(phrases: list[str]) -> list[tuple[str, re.Pattern]]:
    return [(p.strip(), phrase_pattern(p)) for p in phrases if p.strip("* ")]


def _first_hit(patterns: list[tuple[str, re.Pattern]], text: str) -> str | None:
    return next((phrase for phrase, rx in patterns if rx.search(text)), None)


class JobFilter:
    def __init__(self, criteria: Criteria):
        self.c = criteria
        self.title_field = _compile(criteria.title_field)
        self.title_include = _compile(criteria.title_include)
        self.title_exclude = _compile(criteria.title_exclude + criteria.seniority_exclude)
        self.title_exclude_soft = _compile(criteria.title_exclude_soft)
        self.description_exclude = _compile(criteria.description_exclude)
        self.locations = _compile(criteria.locations)

    def hard_exclude(self, job: Job) -> str | None:
        """The title_exclude / seniority_exclude phrase in the title, if any (these always drop a job;
        a job turned down for anything else can get the AI pre-screen's second look)."""
        return _first_hit(self.title_exclude, _normalize(job.title))

    def check_basic(self, job: Job) -> tuple[bool, str]:
        """Title + location. Returns (passed, reason)."""
        title = _normalize(job.title)
        if hit := _first_hit(self.title_exclude, title):
            return False, f"title excludes '{hit}'"
        field = _first_hit(self.title_field, title)
        if not field and (hit := _first_hit(self.title_exclude_soft, title)):
            return False, f"title excludes '{hit}'"
        matched = field or _first_hit(self.title_include, title)
        if (self.title_field or self.title_include) and not matched:
            return False, "title not in include list"
        if self.locations:
            loc = _normalize(job.location)
            is_remote = job.remote or "remote" in loc
            if not (_first_hit(self.locations, loc) or (self.c.allow_remote and is_remote)):
                return False, f"location '{job.location}' not wanted"
        return True, f"title matched '{matched}'" if matched else "passed"

    def check_description(self, job: Job) -> tuple[bool, str]:
        if hit := _first_hit(self.description_exclude, _normalize(job.description)):
            return False, f"description excludes '{hit}'"
        return True, "passed"

    def is_stale(self, job: Job) -> bool:
        age = job.age_days()
        return age is not None and age > self.c.max_age_days


# What happened to each job, in the order of the pipeline (the Filter page's funnel). Each group:
# (id, label, stage) where stage is "out" (turned down), "wait", "in" (fully scored) or "other".
GROUPS = [
    ("too_old", "Posted too long ago", "out"),
    ("hard_exclude", "Title has an exclude or seniority word", "out"),
    ("no_word", "Title has no field or role word", "out"),
    ("location", "Location not on your list", "out"),
    ("ai_title", "AI first look: title clearly not a fit", "out"),
    ("ai_posting", "AI first look: posting clearly not a fit", "out"),
    ("description", "Description has an excluded phrase", "out"),
    ("move", "Needs a move (you chose to stay)", "out"),
    ("low_pay", "Pay under your floor", "out"),
    ("waiting", "Waiting for the AI", "wait"),
    ("pushed", "Scored: pushed to your phone", "in"),
    ("digest", "Scored: in the digest", "in"),
    ("low", "Scored: low fit, stored only", "in"),
    ("other", "Other (closed, errors, duplicates)", "other"),
]
GROUP_LABEL = {g: label for g, label, _ in GROUPS}
GROUP_STAGE = {g: stage for g, _, stage in GROUPS}


def group_of(status: str, reason: str | None) -> str | None:
    """The funnel group of a stored job; None for jobs never judged (open at a board's first poll)."""
    reason = reason or ""
    if status == "baseline":
        return None
    if status == "stale":
        return "too_old"
    if status == "prescreen":
        return "waiting"
    if status == "low_pay":
        return "low_pay"
    if status in ("alert", "alerted"):
        return "pushed"
    if status in ("digest", "digested", "notify_failed"):
        return "digest"
    if status == "low":
        return "low"
    if status == "filtered":
        if reason.startswith("title excludes"):
            return "hard_exclude"
        if reason.startswith("title not in include"):
            return "no_word"
        if reason.startswith("location"):
            return "location"
        if reason.startswith("description excludes"):
            return "description"
        if reason.startswith("needs a move"):
            return "move"
        if reason == "pre-screen: title":
            return "ai_title"
        if reason.startswith("pre-screen:") and "board no longer watched" not in reason:
            return "ai_posting"
    return "other"
