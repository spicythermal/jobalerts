"""Cheap keyword prefilter. Runs before any AI scoring so only plausible jobs cost money."""
from __future__ import annotations

import re

from .config import Criteria
from .models import Job


def _compile(phrases: list[str]) -> list[tuple[str, re.Pattern]]:
    # Word-ish boundaries that still work for phrases like "c++" or ".net".
    return [(p, re.compile(rf"(?<![a-z0-9]){re.escape(p.lower().strip())}(?![a-z0-9])"))
            for p in phrases if p.strip()]


def _first_hit(patterns: list[tuple[str, re.Pattern]], text: str) -> str | None:
    return next((phrase for phrase, rx in patterns if rx.search(text)), None)


class JobFilter:
    def __init__(self, criteria: Criteria):
        self.c = criteria
        self.title_include = _compile(criteria.title_include)
        self.title_exclude = _compile(criteria.title_exclude + criteria.seniority_exclude)
        self.description_exclude = _compile(criteria.description_exclude)
        self.locations = _compile(criteria.locations)

    def check_basic(self, job: Job) -> tuple[bool, str]:
        """Title + location. Returns (passed, reason)."""
        title = job.title.lower()
        if hit := _first_hit(self.title_exclude, title):
            return False, f"title excludes '{hit}'"
        matched = _first_hit(self.title_include, title)
        if self.title_include and not matched:
            return False, "title not in include list"
        if self.locations:
            loc = job.location.lower()
            is_remote = job.remote or "remote" in loc
            if not (_first_hit(self.locations, loc) or (self.c.allow_remote and is_remote)):
                return False, f"location '{job.location}' not wanted"
        return True, f"title matched '{matched}'" if matched else "passed"

    def check_description(self, job: Job) -> tuple[bool, str]:
        if hit := _first_hit(self.description_exclude, job.description.lower()):
            return False, f"description excludes '{hit}'"
        return True, "passed"

    def is_stale(self, job: Job) -> bool:
        age = job.age_days()
        return age is not None and age > self.c.max_age_days
