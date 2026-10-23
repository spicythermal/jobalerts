import httpx
import pytest

from jobalerts.config import Board
from jobalerts.sources import SOURCES, SourceConfigError

from conftest import json_routes, load, mock_http, run


def fetch(source, board, routes):
    async def go():
        http = mock_http(json_routes(routes))
        jobs = await SOURCES[source].fetch(board, http)
        await http.aclose()
        return jobs
    return run(go())


def test_greenhouse_list_and_enrich():
    board = Board("greenhouse", "acme", "Acme", 600)
    routes = {"/boards/acme/jobs/4001": load("greenhouse_job.json"), "/boards/acme/jobs": load("greenhouse_jobs.json")}
    jobs = fetch("greenhouse", board, routes)
    assert [j.title for j in jobs] == ["Senior Network Engineer", "Account Executive"]
    j = jobs[0]
    assert j.key == "greenhouse:acme:4001"
    assert j.remote and j.location == "Remote - US"
    assert j.published_at.isoformat() == "2026-10-20T13:00:00+00:00"
    assert j.department == "Infrastructure"

    async def enrich():
        http = mock_http(json_routes(routes))
        await SOURCES["greenhouse"].enrich(j, board, http)
        await http.aclose()
    run(enrich())
    assert "Build & run our global network." in j.description
    assert "- BGP" in j.description


def test_lever():
    jobs = fetch("lever", Board("lever", "beta", "Beta", 600), {"/v0/postings/beta": load("lever.json")})
    (j,) = jobs
    assert j.title == "Site Reliability Engineer" and j.remote
    assert j.salary == "$150K-190K/per year salary"
    assert "Linux" in j.description and j.department == "Platform"
    assert j.published_at.year == 2026


def test_lever_eu_region_uses_eu_host():
    hosts = []

    def handler(request):
        hosts.append(request.url.host)
        return httpx.Response(200, json=[])

    async def go():
        http = mock_http(handler)
        await SOURCES["lever"].fetch(Board("lever", "x", "X", 600, {"region": "eu"}), http)
        await http.aclose()
    run(go())
    assert hosts == ["api.eu.lever.co"]


def test_ashby_skips_unlisted():
    jobs = fetch("ashby", Board("ashby", "gamma", "Gamma", 600), {"/job-board/gamma": load("ashby.json")})
    (j,) = jobs
    assert j.title == "Platform Engineer" and j.remote
    assert j.location == "Remote (US); Chicago, IL"
    assert j.salary == "$140K – $170K"


def test_smartrecruiters():
    jobs = fetch("smartrecruiters", Board("smartrecruiters", "Delta", "Delta", 600),
                 {"/companies/Delta/postings": load("smartrecruiters.json")})
    (j,) = jobs
    assert j.company == "Delta Corp" and j.location == "Detroit, MI, United States"
    assert j.url == "https://jobs.smartrecruiters.com/Delta/744000012345"


def test_workday_posted_on_and_url():
    board = Board("workday", "nv/Ext", "NV", 1800,
                  {"host": "nv.wd5.myworkdayjobs.com", "tenant": "nv", "site": "Ext", "pages": 2, "search": ""})
    jobs = fetch("workday", board, {"/wday/cxs/nv/Ext/jobs": load("workday.json")})
    assert len(jobs) == 2  # fewer than a full page -> stops after page 1
    today, old = jobs
    assert today.url == "https://nv.wd5.myworkdayjobs.com/Ext/job/Remote-USA/Network-Engineer_JR1"
    assert today.remote and today.age_days() < 0.01
    assert 29.9 < old.age_days() < 30.1


def test_usajobs(monkeypatch):
    monkeypatch.setenv("USAJOBS_API_KEY", "k")
    monkeypatch.setenv("USAJOBS_EMAIL", "me@example.com")
    jobs = fetch("usajobs", Board("usajobs", "it", "usajobs 'it'", 1800, {"keyword": "IT"}, watched=False),
                 {"data.usajobs.gov/api/search": load("usajobs.json")})
    (j,) = jobs
    assert j.board == "" and j.company == "Department of Energy"
    assert j.remote and j.salary == "$99K-129K/yr"
    assert j.published_at.isoformat().startswith("2026-10-21")


def test_usajobs_without_key_is_config_error(monkeypatch):
    monkeypatch.delenv("USAJOBS_API_KEY", raising=False)
    with pytest.raises(SourceConfigError):
        fetch("usajobs", Board("usajobs", "it", "x", 1800, {"keyword": "IT"}), {})


def test_adzuna(monkeypatch):
    monkeypatch.setenv("ADZUNA_APP_ID", "id")
    monkeypatch.setenv("ADZUNA_APP_KEY", "key")
    jobs = fetch("adzuna", Board("adzuna", "net", "x", 3600, {"what": "network", "country": "us"}),
                 {"api.adzuna.com/v1/api/jobs/us/search/1": load("adzuna.json")})
    (j,) = jobs
    assert j.title == "Network Engineer" and j.company == "Epsilon LLC"


def test_jsearch(monkeypatch):
    monkeypatch.setenv("RAPIDAPI_KEY", "k")
    jobs = fetch("jsearch", Board("jsearch", "q", "x", 14400, {"query": "devops"}),
                 {"jsearch.p.rapidapi.com/search": load("jsearch.json")})
    (j,) = jobs
    assert j.remote and j.location == "US" and j.salary == "$120K-150K/year"
