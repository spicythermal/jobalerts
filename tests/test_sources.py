import httpx
import pytest

from jobalerts.config import Board
from jobalerts.sources import SOURCES, SourceConfigError

from conftest import FIXTURES, json_routes, load, mock_http, run


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


def test_ashby_hybrid_is_not_remote():
    data = load("ashby.json")
    listed = next(j for j in data["jobs"] if j.get("isListed", True))
    hybrid = dict(listed, isRemote=True, workplaceType="Hybrid")
    only_flag = dict(listed, id="2", isRemote=True, workplaceType=None)
    jobs = fetch("ashby", Board("ashby", "gamma", "Gamma", 600),
                 {"/job-board/gamma": dict(data, jobs=[hybrid, only_flag])})
    assert [j.remote for j in jobs] == [False, True]


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
    assert j.salary.startswith("$")

    (uk,) = fetch("adzuna", Board("adzuna", "net-gb", "x", 3600, {"what": "network", "country": "gb"}),
                  {"api.adzuna.com/v1/api/jobs/gb/search/1": load("adzuna.json")})
    assert uk.salary.startswith("GBP ")


def test_jsearch(monkeypatch):
    monkeypatch.setenv("RAPIDAPI_KEY", "k")
    jobs = fetch("jsearch", Board("jsearch", "q", "x", 14400, {"query": "devops"}),
                 {"jsearch.p.rapidapi.com/search": load("jsearch.json")})
    (j,) = jobs
    assert j.remote and j.location == "US" and j.salary == "$120K-150K/year"


ULTIPRO_URL = "https://recruiting2.ultipro.com/TRI1021TRIAE/JobBoard/49386e5d-f909-4648-8335-415ce97e3f15"


def test_ultipro_posts_search_and_builds_detail_urls():
    from jobalerts.config import Company, Config
    cfg = Config(companies=[Company(name="TAE Technologies", ats="ultipro", url=ULTIPRO_URL + "/?q=")])
    (board,) = [b for b in cfg.boards() if b.watched]
    assert board.slug == "TRI1021TRIAE/49386e5d"
    jobs = fetch("ultipro", board, {"/JobBoardView/LoadSearchResults": load("ultipro.json")})
    j = jobs[0]
    assert [x.title for x in jobs] == ["Senior Controls Engineer - PLC", "Sr. Design Engineer"]
    assert j.url == ULTIPRO_URL + "/OpportunityDetail?opportunityId=" + j.job_id
    assert j.location == "Foothill Ranch, California, United States" and not j.remote
    assert j.published_at.date().isoformat() == "2026-10-23"
    assert j.department == "Engineering" and j.description


def test_personio_xml():
    xml = (FIXTURES / "personio.xml").read_text(encoding="utf-8")

    async def go():
        http = mock_http(lambda request: httpx.Response(200, text=xml))
        jobs = await SOURCES["personio"].fetch(Board("personio", "gauss", "Gauss Fusion", 600), http)
        await http.aclose()
        return jobs
    j = run(go())[0]
    assert j.title == "Design & Analysis Engineer" and j.job_id == "2117264"
    assert j.url == "https://gauss.jobs.personio.com/job/2117264"
    assert j.location == "Munich" and j.published_at.year == 2025
    assert "Main responsibilities" in j.description and "stellarator" in j.description


def test_workable():
    jobs = fetch("workable", Board("workable", "quandela", "Quandela", 600),
                 {"/widget/accounts/quandela": load("workable.json")})
    j = jobs[0]
    assert j.title == "Cloud DevOps Engineer" and j.url == "https://apply.workable.com/j/7445534397"
    assert j.location == "Massy, Île-de-France, France" and not j.remote
    assert j.published_at.date().isoformat() == "2026-10-16"
    assert j.description.startswith("Quandela") and jobs[1].department == "Business & Marketing"


def test_bamboohr_list_and_enrich():
    board = Board("bamboohr", "satellogic", "Satellogic", 600)
    routes = {"/careers/554/detail": load("bamboohr_detail.json"), "/careers/list": load("bamboohr.json")}
    j = fetch("bamboohr", board, routes)[0]
    assert j.title == "Hardware Design Engineer" and j.location == "Buenos Aires, Argentina"
    assert j.url == "https://satellogic.bamboohr.com/careers/554" and j.published_at is None

    async def enrich():
        http = mock_http(json_routes(routes))
        await SOURCES["bamboohr"].enrich(j, board, http)
        await http.aclose()
    run(enrich())
    assert j.published_at.date().isoformat() == "2026-07-13" and j.description


def test_rippling_list_and_enrich():
    board = Board("rippling", "zap-energy-careers", "Zap Energy", 600)
    routes = {"/jobs/5ac2acce": load("rippling_detail.json"), "/jobs": load("rippling.json")}
    jobs = fetch("rippling", board, routes)
    j = jobs[0]
    assert [x.department for x in jobs] == ["Executives", "Fission Technology"]
    assert j.location == "Everett, WA" and not j.remote and j.url.endswith(j.job_id)

    async def enrich():
        http = mock_http(json_routes(routes))
        await SOURCES["rippling"].enrich(j, board, http)
        await http.aclose()
    run(enrich())
    assert j.published_at.year == 2026 and "<" not in j.description and j.description


def test_breezy():
    jobs = fetch("breezy", Board("breezy", "zeno-power", "Zeno Power", 600), {"zeno-power.breezy.hr/json": load("breezy.json")})
    j = jobs[1]
    assert j.title == "Analysis Manager" and j.location == "Seattle, WA" and not j.remote
    assert j.salary == "$175,000 – $225,000 / year" and j.department == "Engineering"
    assert j.published_at.date().isoformat() == "2026-10-02"


def test_successfactors_rss():
    from jobalerts.config import Company, Config
    cfg = Config(companies=[Company(name="BWXT", ats="successfactors", url="https://careers.bwxt.com/", search="physicist")])
    (board,) = cfg.boards()
    assert board.slug == "careers.bwxt.com"
    rss = (FIXTURES / "successfactors.xml").read_text(encoding="utf-8")
    urls = []

    def handler(request):
        urls.append(str(request.url))
        return httpx.Response(200, text=rss)

    async def go():
        http = mock_http(handler)
        jobs = await SOURCES["successfactors"].fetch(board, http)
        await http.aclose()
        return jobs
    jobs = run(go())
    assert urls == ["https://careers.bwxt.com/services/rss/job/?locale=en_US&keywords=physicist"]
    assert [(j.title, j.location) for j in jobs] == [
        ("Intern - Analytical Services (Year Round)", "Erwin, TN, US"), ("Quality Engineer", "Jonesborough, TN, US")]
    j = jobs[1]
    assert j.job_id == "1434475900" and "?" not in j.url and j.url.endswith("/1434475900/")
    assert j.published_at.isoformat() == "2026-10-27T07:00:00+00:00" and "BWX Technologies" in j.description
