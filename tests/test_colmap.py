import json
import re

import pytest

from jobalerts import col, colmap
from jobalerts.cli import main
from jobalerts.config import load_config
from jobalerts.http import HttpClient
from jobalerts.notify import Notifier
from jobalerts.scheduler import Runner
from jobalerts.store import Store

from conftest import REPO, run, with_reference_home


@pytest.fixture(scope="module")
def pay():
    cfg = with_reference_home(load_config(REPO / "config" / "config.yaml"))
    runner = Runner(cfg, Store(":memory:"), HttpClient(), Notifier(cfg.notify, dry_run=True))
    assert runner.pay is not None
    return runner.pay


def job(n, location, **kw):
    return {"title": f"Job {n}", "company": "Acme", "location": location, "url": f"https://x/{n}", "score": 80,
            "pay_index": 1.2, "pay_low": 1.1, "pay_high": 1.3, "buy_index": 0.9, "buy_low": 0.8, "buy_high": 1.0,
            "story_ids": ["gun"], **kw}


def test_every_metro_is_placed_and_matches_the_pay_index(pay):
    data = colmap.build_map_data(pay)
    assert len(data["metros"]) == len(pay.metros) == 387
    home = [m for m in data["metros"] if m["home"]]
    assert len(home) == 1 and home[0]["be_rent"] == pytest.approx(155_000, abs=1_000)
    assert home[0]["be_buy"] == pytest.approx(155_000, abs=1_000)   # buying at home = staying in the house
    i = data["salaries"].index(250_000)
    by = {m["name"]: m for m in data["metros"]}
    for loc in ("Albuquerque, NM", "Austin, TX", "Boston, MA"):   # no city blend there: map == pay index
        r = pay.index(col.Pay(250_000, 250_000, "USD", "year"), loc)
        m = by[colmap._short(r.place.metro)]
        assert m["rent"][i] == pytest.approx(r.index, abs=0.001) and m["buy"][i] == pytest.approx(r.buy_index, abs=0.001)
    for m in data["metros"]:                                        # more pay is never worse
        assert m["rent"] == sorted(m["rent"])
    for name in list(pay.metros)[::25]:                             # break-even really is where the index is 1.00
        m = by[colmap._short(name)]
        place = pay.metro_place(name)
        assert pay.ratios(m["be_rent"], place)[0] == pytest.approx(1.0, abs=0.002)
        if m["be_buy"]:
            assert pay.ratios(m["be_buy"], place)[1] == pytest.approx(1.0, abs=0.002)


def test_jobs_land_on_their_metro_or_in_the_list(pay):
    jobs = [job(1, "Palo Alto, CA"), job(2, "San Jose, CA"), job(3, "Remote"), job(4, "Bayeux, France"),
            job(5, "Nowhere")]
    steps = [job(2, "San Jose, CA", step_up=80), job(6, "Boston, MA", step_up=85)]
    data = colmap.build_map_data(pay, jobs, steps)
    placed = {j["title"]: j for j in data["jobs"]}
    assert set(placed) == {"Job 1", "Job 2", "Job 3", "Job 6"}      # Job 2 shows once, as a top job
    assert placed["Job 2"]["kind"] == "top" and placed["Job 6"]["kind"] == "step"
    assert placed["Job 3"]["metro"].startswith("Portland")          # remote = living at home
    assert (placed["Job 1"]["lat"], placed["Job 1"]["lon"]) != (placed["Job 2"]["lat"], placed["Job 2"]["lon"])
    assert [j["title"] for j in data["unplaced"]] == ["Job 4", "Job 5"]
    assert "FRA" in data["unplaced"][0]["where"] and placed["Job 1"]["stories"] == ["gun"]


def test_render_inlines_the_data_safely(pay):
    data = colmap.build_map_data(pay, [job(1, "Austin, TX", title="</script><script>alert(1)</script>")])
    html = colmap.render(data)
    assert "/*__MAP_DATA__*/" not in html
    assert html.count("</script") == 3                              # only the page's own three script tags close
    blob = re.search(r"const D = (\{.*?\});\n", html, re.S).group(1)
    assert json.loads(blob.replace("<\\/", "</"))["jobs"][0]["title"].startswith("</script>")
    assert "leaflet/1.9.4" in html and "us-atlas@3.0.1" in html


def test_only_web_links_reach_the_map(pay):
    data = colmap.build_map_data(pay, [job(1, "Austin, TX", url="javascript:alert(1)"),
                                       job(2, "Nowhere", url="data:text/html,x"), job(3, "Boston, MA")])
    urls = {j["title"]: j["url"] for j in data["jobs"] + data["unplaced"]}
    assert urls == {"Job 1": "#", "Job 2": "#", "Job 3": "https://x/3"}


def test_cli_writes_the_page(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)                                     # no database here: map without jobs
    out = tmp_path / "out" / "map.html"
    assert main(["-c", str(REPO / "config" / "config.yaml"), "map", "--out", str(out)]) == 0
    assert "const D = {" in out.read_text() and "no database yet" in capsys.readouterr().out


def test_dashboard_loop_keeps_the_map_page_fresh():
    import asyncio
    cfg = load_config(REPO / "config" / "config.yaml")
    runner = Runner(cfg, Store(":memory:"), HttpClient(), Notifier(cfg.notify, dry_run=True))

    async def go():
        task = asyncio.create_task(runner._dashboard_loop())
        for _ in range(100):
            await asyncio.sleep(0.05)
            if runner.map_html:
                break
        task.cancel()
        return runner.map_html
    assert "const D = {" in run(go())
    built = runner._map_built
    assert run(runner.current_map()) == runner.map_html and runner._map_built == built  # fresh: not rebuilt
