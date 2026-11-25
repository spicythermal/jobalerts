"""Boards page: status per board, and editing the watch list (comments kept, loops follow live)."""
import asyncio
import re
from html import unescape

import pytest
from fastapi.testclient import TestClient

from jobalerts import discover, vault, watchlist
from jobalerts.config import Config
from jobalerts.http import HttpClient
from jobalerts.notify import Notifier
from jobalerts.scheduler import Runner
from jobalerts.store import Store
from jobalerts.web.app import create_app

PASSWORD = "boards page test password"
LIST = """# My watch list
# fusion first
companies:
  # --- fusion
  - {"name": "Helion", "ats": "ashby", "slug": "helion", "tier": "normal"}  # hiring a lot
  - {"name": "Zap Energy", "ats": "rippling", "slug": "zap-energy", "tier": "normal"}
  # --- big ones
  - {"name": "NVIDIA", "ats": "workday", "url": "https://nvidia.wd5.myworkdayjobs.com/External", "tier": "slow", "enabled": false}
"""


def test_watchlist_edits_keep_comments(tmp_path):
    path = tmp_path / "companies.yaml"
    path.write_text(LIST)
    watchlist.change(path, 0, {"name": "Helion", "ats": "ashby", "slug": "helion", "tier": "priority"})
    text = path.read_text()
    assert '"tier": "priority"}  # hiring a lot' in text and "# --- big ones" in text and "# My watch list" in text
    watchlist.change(path, 1, None)
    assert "Zap Energy" not in path.read_text() and len(watchlist.load(path)) == 2
    assert watchlist.add(path, {"name": "TAE", "ats": "greenhouse", "slug": "tae", "tier": "normal"})
    assert not watchlist.add(path, {"name": "Helion again", "ats": "ashby", "slug": "helion", "tier": "slow"})
    assert [e["name"] for e in watchlist.load(path)] == ["Helion", "NVIDIA", "TAE"]
    with pytest.raises(ValueError):
        watchlist.add(path, {"name": "Bad", "ats": "linkedin", "slug": "x"})


def test_watchlist_rewrites_hand_written_entries(tmp_path):
    path = tmp_path / "companies.yaml"
    path.write_text("# header\ncompanies:\n  - name: Helion\n    ats: ashby\n    slug: helion\n")
    watchlist.change(path, 0, {"name": "Helion", "ats": "ashby", "slug": "helion", "tier": "slow"})
    assert path.read_text().startswith("# header\ncompanies:\n  - {") and watchlist.load(path)[0]["tier"] == "slow"


@pytest.fixture
def env(tmp_path, monkeypatch):
    for name in vault.SECRETS:
        monkeypatch.delenv(name, raising=False)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "companies.yaml").write_text(LIST)
    cfg = Config(config_dir=tmp_path / "config", database=str(tmp_path / "data" / "jobs.db"),
                 pay={"enabled": False}, aggregators={"theirstack": {"enabled": True, "searches": [{"name": "physics"}]}})
    cfg.companies = [__import__("jobalerts.config", fromlist=["Company"]).Company.model_validate(e)
                     for e in watchlist.load(tmp_path / "data" / "companies.yaml")]
    store = Store(cfg.database)
    store.mark_board_ok("ashby:helion", 42)
    store.mark_board_failed("rippling:zap-energy", "HTTPStatusError: 500")
    runner = Runner(cfg, store, HttpClient(), Notifier(cfg.notify, dry_run=True))
    started = []

    async def fake_loop(board):
        started.append(board.key)
        await asyncio.sleep(3600)
    monkeypatch.setattr(runner, "_board_loop", fake_loop)
    app = create_app(runner, b"b" * 32)
    with TestClient(app, follow_redirects=False) as client:   # one event loop for the whole test
        token = re.search(r'name="csrf" value="([^"]+)"', client.get("/setup").text).group(1)
        client.post("/setup", data={"csrf": token, "code": app.state.web.setup_code, "password": PASSWORD, "again": PASSWORD})
        token = re.search(r'name="csrf" value="([^"]+)"', client.get("/boards").text).group(1)
        yield runner, client, token, started, tmp_path / "data" / "companies.yaml"
        for _, task in runner.board_tasks.values():
            task.cancel()
    store.close()


def test_status_rows(env):
    runner, client, token, started, path = env
    html = unescape(client.get("/boards").text)
    rows = {name: state for state, name in re.findall(r'<tr class="state-(\w+)">\s*<td>([^<]+)</td>', html)}
    assert rows == {"Helion": "ok", "Zap Energy": "failing", "NVIDIA": "disabled", "theirstack 'physics'": "needs_key"}
    assert "HTTPStatusError: 500" in html and "needs THEIRSTACK_API_KEY" in html and ">42<" in html
    assert client.get("/boards/table").status_code == 200


def test_adding_a_board_found_by_discovery(env, monkeypatch):
    runner, client, token, started, path = env

    async def fake_find(name, http):
        return [discover.Found("greenhouse", "tae", "TAE Technologies", 57)]
    monkeypatch.setattr(discover, "find_by_name", fake_find)
    html = client.post("/boards/discover", data={"target": "TAE"}, headers={"X-CSRF-Token": token}).text
    assert "57 open jobs" in html and 'name="slug" value="tae"' in html
    r = client.post("/boards/add", data={"csrf": token, "name": "TAE Technologies", "ats": "greenhouse", "slug": "tae",
                                         "tier": "priority"})
    assert r.headers["location"] == "/boards?saved=added"
    assert watchlist.load(path)[-1] == {"name": "TAE Technologies", "ats": "greenhouse", "slug": "tae", "tier": "priority"}
    assert "TAE Technologies" in runner.cfg.watched_companies()
    assert client.post("/boards/add", data={"csrf": token, "name": "x", "ats": "greenhouse", "slug": "tae"}).headers[
        "location"] == "/boards?saved=already"


def test_edits_follow_into_the_running_loops(env):
    runner, client, token, started, path = env

    async def start():
        runner.running = True
        runner.sync_boards()
        await asyncio.sleep(0)
    client.portal.call(start)
    assert set(runner.board_tasks) == {"ashby:helion", "rippling:zap-energy"}       # NVIDIA is off; no TheirStack key
    client.post("/boards/2/enabled", data={"csrf": token, "enabled": "1"})
    assert "enabled" not in watchlist.load(path)[2] and "nvidia/External" in " ".join(runner.board_tasks)
    client.post("/boards/0/tier", data={"csrf": token, "tier": "slow"})
    assert runner.board_tasks["ashby:helion"][0].interval == runner.cfg.polling.tiers["slow"]   # restarted
    client.post("/boards/1/remove", data={"csrf": token})
    assert "rippling:zap-energy" not in runner.board_tasks and len(watchlist.load(path)) == 2
    assert client.post("/boards/9/remove", data={"csrf": token}).status_code == 404
    assert client.post("/boards/0/tier", data={"csrf": token, "tier": "turbo"}).status_code == 400
    assert client.post("/boards/0/remove").status_code == 403
