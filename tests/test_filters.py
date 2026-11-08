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


def test_plurals_wildcards_and_spacing():
    f = JobFilter(Criteria(title_include=["beam", "*physicist", "research*", "group lead*"]))
    assert f.check_basic(job("Design Engineer - Neutral Beams"))[0]
    assert f.check_basic(job("Cryophysicist"))[0] and f.check_basic(job("Physicists wanted"))[0]
    assert f.check_basic(job("QEC Researcher"))[0]
    assert f.check_basic(job("Group  Leader,   Ion Optics"))[0]  # runs of spaces don't matter
    assert not f.check_basic(job("Beamforming Engineer"))[0]     # no * at the end: whole word only


def test_field_words_beat_soft_excludes_only():
    f = JobFilter(Criteria(title_field=["plasma"], title_include=["engineer"], title_exclude=["intern"],
                           title_exclude_soft=["data scientist"]))
    assert f.check_basic(job("Senior Applied Plasma Data Scientist"))[0]
    ok, why = f.check_basic(job("Data Scientist, Marketing"))
    assert not ok and "data scientist" in why
    assert not f.check_basic(job("Plasma Engineering Intern"))[0]  # a hard exclude still wins
    assert f.check_basic(job("Plasma Physicist"))[1] == "title matched 'plasma'"


def test_shipped_config_catches_measured_misses():
    """Titles the old filter dropped in a replay of all watched boards (2026-11-06)."""
    from conftest import REPO
    from jobalerts.config import load_config
    f = JobFilter(load_config(REPO / "config" / "config.yaml").criteria)
    for title in ["Cryophysicist", "Laser Diagnostician", "Senior Design Engineer - Neutral Beams", "Researcher",
                  "Control Software Engineer - Particle Accelerators", "Principal Applied Plasma Data Scientist",
                  "Process Engineer - Ion Implant", "Neutronics Engineer", "Technical Project Manager - Implosion",
                  "Applied Engineering Mechanics Group Leader"]:
        assert f.check_basic(job(title))[0], title
    for title in ["Clinical Support Specialist - West", "Nuclear Welder (GTAW, SMAW)", "Working Student - Laser PMO",
                  "Sourcing Manager, Mechanical & Optical Commodities", "Master Thesis - Plasma Simulation"]:
        assert not f.check_basic(job(title))[0], title


def test_shipped_config_drops_non_english_internships():
    from conftest import REPO
    from jobalerts.config import load_config
    f = JobFilter(load_config(REPO / "config" / "config.yaml").criteria)
    assert not f.check_basic(job("Stage Bac+5 - Pilotage par IA d'une chaîne de simulation F/H", "Elancourt"))[0]
    assert not f.check_basic(job("Werkstudent Physik (m/w/d)", "München"))[0]
    for title in ["STAGE - Ingénieure / Ingénieur GNC", "STAGE- Ingénieur Matériaux", "STAGE  –  Ingénieur Optique",
                  "STAGE 2027 - Ingénieur Métrologie", "Stage Ingénieur Laser (H/F)"]:
        assert not f.check_basic(job(title, "Toulouse"))[0], title
    # a rocket stage is not a French internship
    assert f.check_basic(job("Stage Test Engineer - Plasma Thrusters"))[0]
    assert f.check_basic(job("Physicist, Stage-Gate R&D"))[0]
    assert f.check_basic(job("Physiker für Beschleunigertechnik (m/w/d)", "Darmstadt"))[0]
    assert f.check_basic(job("Accelerator Physicist", "Lund, Sweden"))[0]  # no location filter any more


def test_real_config_reads_french_and_dutch_beam_titles():
    from conftest import REPO
    from jobalerts.config import load_config
    f = JobFilter(load_config(REPO / "config" / "config.yaml").criteria)
    for title in ("Ingénieur Sources d’Ions ECR H/F", "Responsable faisceaux d'électrons", "Physicien accélérateur",
                  "Ingénieur optique électronique", "Elektronenmicroscopie specialist",
                  "Integration Engineer, Ultravide", "Ingénieur de recherche en physique"):
        assert f.check_basic(job(title))[0], title
    for title in ("Technicien de maintenance accélérateur", "Assembleur en mécanique de précision",
                  "Acheteur Industriel (Mécanique de précision)", "Câbleur faisceaux électriques",
                  "Stage ingénieur physique"):
        assert not f.check_basic(job(title))[0], title
