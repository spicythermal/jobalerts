"""Source adapter registry, keyed by the `ats` / aggregator name used in config."""
from .adzuna import Adzuna
from .ashby import Ashby
from .base import Source, SourceConfigError
from .greenhouse import Greenhouse
from .jsearch import JSearch
from .lever import Lever
from .smartrecruiters import SmartRecruiters
from .usajobs import USAJobs
from .workday import Workday

SOURCES: dict[str, Source] = {s.name: s for s in (
    Greenhouse(), Lever(), Ashby(), SmartRecruiters(), Workday(), USAJobs(), Adzuna(), JSearch())}

__all__ = ["SOURCES", "Source", "SourceConfigError"]
