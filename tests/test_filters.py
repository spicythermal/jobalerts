from datetime import timedelta

from jobalerts.config import Criteria
from jobalerts.filters import JobFilter
from jobalerts.models import Job, utcnow


def job(title, location="Remote", remote=False, description="", age_days=1):
    return Job("greenhouse", "x", "1", "Acme", title, "https://x", location=location, remote=remote,
               description=description, published_at=utcnow() - timedelta(days=age_days))


F = JobFilter(Criteria(
    title_include=["network engineer", "sre", "c++"],
    title_exclude=["sales"],
    seniority_exclude=["intern"],
    description_exclude=["secret clearance"],
    locations=["michigan", "detroit"],
    allow_remote=True,
    max_age_days=7,
))


def test_title_include_whole_words():
    assert F.check_basic(job("Senior Network Engineer"))[0]
    assert F.check_basic(job("SRE II"))[0]
    assert F.check_basic(job("C++ Developer"))[0]
    assert not F.check_basic(job("Presales Engineer"))[0]          # not in include list
    assert not F.check_basic(job("Network Engineering Manager"))[0]  # "engineer" != "engineering"


def test_excludes_win():
    ok, why = F.check_basic(job("Network Engineer - Sales"))
    assert not ok and "sales" in why
    assert not F.check_basic(job("Network Engineer Intern"))[0]


def test_location_rules():
    assert F.check_basic(job("Network Engineer", "Detroit, MI"))[0]
    assert F.check_basic(job("Network Engineer", "Austin, TX", remote=True))[0]
    assert F.check_basic(job("Network Engineer", "Remote - US"))[0]
    ok, why = F.check_basic(job("Network Engineer", "Austin, TX"))
    assert not ok and "location" in why
    no_remote = JobFilter(Criteria(locations=["detroit"], allow_remote=False))
    assert not no_remote.check_basic(job("Anything", "Remote"))[0]


def test_description_and_staleness():
    assert not F.check_description(job("SRE", description="Requires active Secret Clearance."))[0]
    assert F.check_description(job("SRE", description="No clearance needed"))[0]
    assert F.is_stale(job("SRE", age_days=10))
    assert not F.is_stale(job("SRE", age_days=2))
    undated = job("SRE")
    undated.published_at = None
    assert not F.is_stale(undated)


def test_empty_criteria_passes_everything():
    assert JobFilter(Criteria()).check_basic(job("Anything", "Mars"))[0]
