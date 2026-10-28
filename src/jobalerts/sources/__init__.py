"""Source adapter registry, keyed by the `ats` / aggregator name used in config."""
from .adzuna import Adzuna
from .ashby import Ashby
from .bamboohr import BambooHR
from .breezy import Breezy
from .base import Source, SourceConfigError
from .greenhouse import Greenhouse
from .jsearch import JSearch
from .lever import Lever
from .personio import Personio
from .rippling import Rippling
from .smartrecruiters import SmartRecruiters
from .successfactors import SuccessFactors
from .ultipro import UltiPro
from .usajobs import USAJobs
from .workable import Workable
from .workday import Workday

SOURCES: dict[str, Source] = {s.name: s for s in (
    Greenhouse(), Lever(), Ashby(), SmartRecruiters(), Workday(), UltiPro(), Personio(), Workable(),
    BambooHR(), Rippling(), Breezy(), SuccessFactors(), USAJobs(), Adzuna(), JSearch())}


def without_missing_keys(boards):
    """Drop boards whose source has no API key in .env, so an enabled-but-unset aggregator sits idle
    instead of failing. Returns (boards to poll, {source: [missing .env keys]})."""
    skipped: dict[str, list[str]] = {}
    kept = []
    for b in boards:
        missing = SOURCES[b.source].missing_keys()
        if missing:
            skipped[b.source] = missing
        else:
            kept.append(b)
    return kept, skipped


__all__ = ["SOURCES", "Source", "SourceConfigError", "without_missing_keys"]
