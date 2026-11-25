import json

import httpx

from jobalerts import discover
from jobalerts.config import Notify
from jobalerts.models import Job
from jobalerts.notify import Notifier

from conftest import json_routes, mock_http, run


def test_slug_candidates():
    assert discover.slug_candidates("Acme Labs, Inc.") == ["acme", "acmelabs", "acmelabsinc"]
    assert discover.slug_candidates("The Trade Desk")[:2] == ["tradedesk", "trade-desk"]


def test_links_in_careers_html():
    html = '''<iframe src="https://boards.greenhouse.io/embed/job_board?for=acme"></iframe>
              <a href="https://jobs.lever.co/acme-co/123">x</a>
              <a href="https://jobs.eu.lever.co/acme-eu">x</a>
              <a href="https://acme.wd5.myworkdayjobs.com/en-US/AcmeCareers/job/1">y</a>'''
    assert discover.links_in(html) == [
        ("greenhouse", "acme"), ("lever", "acme-co"), ("lever-eu", "acme-eu"),
        ("workday", "https://acme.wd5.myworkdayjobs.com/en-US/AcmeCareers")]


def test_links_in_more_platforms():
    html = '''<a href="https://recruiting2.ultipro.com/ACM1001ACME/JobBoard/49386e5d-f909-4648-8335-415ce97e3f15/?q=">u</a>
              <script src="https://acme-gmbh.jobs.personio.de/widget.js"></script>
              <a href="https://apply.workable.com/acme/j/123">w</a>
              <a href="https://acme.bamboohr.com/careers">b</a> <a href="https://www.bamboohr.com">ad</a>
              <a href="https://ats.rippling.com/acme-careers/jobs">r</a>
              <a href="https://acme-inc.breezy.hr/">z</a>'''
    assert discover.links_in(html) == [
        ("ultipro", "https://recruiting2.ultipro.com/ACM1001ACME/JobBoard/49386e5d-f909-4648-8335-415ce97e3f15"),
        ("personio", "acme-gmbh"), ("workable", "acme"), ("bamboohr", "acme"),
        ("rippling", "acme-careers"), ("breezy", "acme-inc")]


def test_find_by_url_ultipro_entry_keeps_board_url():
    url = "https://recruiting.ultipro.com/ACM1001ACME/JobBoard/49386e5d-f909-4648-8335-415ce97e3f15"
    routes = {"/JobBoardView/LoadSearchResults": {"opportunities": [{"Id": "1"}], "totalCount": 7}}

    async def go():
        http = mock_http(json_routes(routes))
        found = await discover.find_by_url(url + "/?q=", http, name="Acme")
        await http.aclose()
        return found

    [f] = run(go())
    assert f.jobs == 7 and f.entry("slow") == {"name": "Acme", "ats": "ultipro", "url": url, "tier": "slow"}


def test_find_by_url_probes_lever_eu_host():
    routes = {"api.eu.lever.co/v0/postings/acme-eu": [{"id": "1"}]}

    async def go():
        http = mock_http(json_routes(routes))
        found = await discover.find_by_url("https://jobs.eu.lever.co/acme-eu", http, name="Acme EU")
        await http.aclose()
        return found

    [f] = run(go())
    assert f.entry() == {"name": "Acme EU", "ats": "lever", "slug": "acme-eu", "region": "eu", "tier": "normal"}


def test_find_by_name_probes_platforms():
    routes = {
        "boards-api.greenhouse.io/v1/boards/acme/jobs": {"jobs": [{"id": 1}, {"id": 2}]},
        "api.ashbyhq.com/posting-api/job-board/acme": {"jobs": []},          # empty -> not counted
        "api.smartrecruiters.com/v1/companies/acme/postings": {"totalFound": 0},  # SR answers for any id
    }

    async def go():
        http = mock_http(json_routes(routes))
        found = await discover.find_by_name("Acme", http)
        await http.aclose()
        return found

    found = run(go())
    assert [(f.ats, f.slug, f.jobs) for f in found] == [("greenhouse", "acme", 2)]

    routes["boards-api.greenhouse.io/v1/boards/acme/jobs"] = {"jobs": []}  # empty greenhouse board too
    assert run(go()) == []


def test_append_to_companies(tmp_path):
    path = tmp_path / "companies.yaml"
    path.write_text("companies:\n  - {name: Old, ats: lever, slug: old}\n", encoding="utf-8")
    f = discover.Found("greenhouse", "acme", "Acme", 3)
    assert discover.append_to_companies(path, f, "priority")
    assert not discover.append_to_companies(path, f, "priority")  # no duplicates
    assert not discover.append_to_companies(path, discover.Found("lever", "old", "Old", 1), "normal")
    import yaml
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert data["companies"][-1] == {"name": "Acme", "ats": "greenhouse", "slug": "acme", "tier": "priority"}


def test_ntfy_payload_and_auth():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"id": "x"})

    async def go():
        n = Notifier(Notify(server="https://ntfy.example.com/", topic="t", token="tok"),
                     transport=httpx.MockTransport(handler))
        job = Job("lever", "a", "1", "Acme", "SRE", "https://jobs/1", location="Detroit, MI", remote=True,
                  salary="$150K")
        await n.job_alert(job)
        await n.aclose()

    run(go())
    (req,) = requests
    assert str(req.url) == "https://ntfy.example.com"
    assert req.headers["Authorization"] == "Bearer tok"
    body = json.loads(req.content)
    assert body["topic"] == "t" and body["title"] == "Acme: SRE" and body["click"] == "https://jobs/1"
    assert body["priority"] == 4
    assert body["actions"][0]["url"] == "https://jobs/1"
    assert body["message"].startswith("Detroit, MI · Remote · $150K")


def test_no_topic_means_dry_run():
    n = Notifier(Notify(topic=""))
    run(n.send("t", "m"))
    assert n.dry_run and n.sent[0]["title"] == "t"
    run(n.aclose())


def test_discover_file_batch_adds_each_found_board(tmp_path, monkeypatch, capsys):
    from jobalerts import cli
    from jobalerts.config import Config
    (tmp_path / "companies.yaml").write_text("companies:\n", encoding="utf-8")
    (tmp_path / "list.txt").write_text(
        "# comment\nAcme  # inline comment\n\nBig Corp | https://big.wd1.myworkdayjobs.com/External\nNobody\n",
        encoding="utf-8")
    assert cli.read_targets(str(tmp_path / "list.txt")) == [
        ("Acme", ""), ("Big Corp", "https://big.wd1.myworkdayjobs.com/External"), ("Nobody", "")]

    async def by_name(name, http):
        return [discover.Found("greenhouse", "acme", name, 4)] if name == "Acme" else []

    async def by_url(url, http, name=""):
        return [discover.Found("workday", "big/External", name, 900, url)]

    monkeypatch.setattr(discover, "find_by_name", by_name)
    monkeypatch.setattr(discover, "find_by_url", by_url)
    args = cli.build_parser().parse_args(["discover", "--file", str(tmp_path / "list.txt"), "--add"])
    assert run(cli.cmd_discover(Config(config_dir=tmp_path, companies_file="companies.yaml"), args)) == 0
    import yaml
    entries = yaml.safe_load((tmp_path / "companies.yaml").read_text(encoding="utf-8"))["companies"]
    assert entries == [
        {"name": "Acme", "ats": "greenhouse", "slug": "acme", "tier": "normal"},
        {"name": "Big Corp", "ats": "workday", "url": "https://big.wd1.myworkdayjobs.com/External", "tier": "slow"},
    ]
    assert "2 of 3 found. No supported board for: Nobody" in capsys.readouterr().out


def test_find_by_url_recognises_successfactors_site():
    rss = '<?xml version="1.0"?><rss version="2.0"><channel><item><title>A</title></item></channel></rss>'

    def handler(request):
        if request.url.path.startswith("/services/rss/job"):
            return httpx.Response(200, text=rss)
        return httpx.Response(200, text='<link href="https://rmkcdn.successfactors.com/abc/site.css">')

    async def go():
        http = mock_http(handler)
        found = await discover.find_by_url("https://careers.acme.com/", http, name="Acme")
        await http.aclose()
        return found

    [f] = run(go())
    assert (f.slug, f.jobs) == ("careers.acme.com", 1)
    assert f.entry() == {"name": "Acme", "ats": "successfactors", "url": "https://careers.acme.com", "tier": "normal"}
