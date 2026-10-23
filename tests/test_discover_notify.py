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
              <a href="https://acme.wd5.myworkdayjobs.com/en-US/AcmeCareers/job/1">y</a>'''
    assert discover.links_in(html) == [
        ("greenhouse", "acme"), ("lever", "acme-co"),
        ("workday", "https://acme.wd5.myworkdayjobs.com/en-US/AcmeCareers")]


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
