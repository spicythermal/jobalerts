"""Updates from the repository: what's new, refusing when something of yours is in the way, installing
with a backup and a rollback, and the service's notify / install modes."""
import re
import subprocess

import pytest
from fastapi.testclient import TestClient

from jobalerts import updater
from jobalerts.store import Store
from jobalerts.updater import UpdateStatus

from conftest import run
from test_pipeline import env  # noqa: F401 (fixture)


def sh(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repos(tmp_path, monkeypatch):
    """An 'origin' with one commit, your clone of it, and a second clone to publish new versions from."""
    for k, v in (("GIT_AUTHOR_NAME", "t"), ("GIT_AUTHOR_EMAIL", "t@example.com"), ("GIT_COMMITTER_NAME", "t"),
                 ("GIT_COMMITTER_EMAIL", "t@example.com")):
        monkeypatch.setenv(k, v)
    origin = tmp_path / "origin.git"
    sh(tmp_path, "init", "--quiet", "--bare", "-b", "main", str(origin))
    dev = tmp_path / "dev"
    sh(tmp_path, "clone", "--quiet", str(origin), str(dev))
    (dev / "pyproject.toml").write_text("[project]\nname='x'\n")
    (dev / ".gitignore").write_text("data/\n")
    (dev / "app.py").write_text("v = 1\n")
    sh(dev, "add", "-A")
    sh(dev, "commit", "--quiet", "-m", "first")
    sh(dev, "push", "--quiet", "origin", "main")
    mine = tmp_path / "mine"
    sh(tmp_path, "clone", "--quiet", str(origin), str(mine))
    return dev, mine


def publish(dev, message="second", **files):
    for name, text in files.items():
        path = dev / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    sh(dev, "add", "-A")
    sh(dev, "commit", "--quiet", "-m", message)
    sh(dev, "push", "--quiet", "origin", "main")


def test_check_lists_whats_new(repos):
    dev, mine = repos
    status = updater.check(mine)
    assert not status.available and status.problem == "" and status.upstream == "origin/main"
    publish(dev, "Better jobs page", **{"app.py": "v = 2\n"})
    publish(dev, "Fix a typo", **{"README": "hi\n"})
    status = updater.check(mine)
    assert status.behind == 2 and status.can_install
    assert [c.split(" ", 1)[1] for c in status.commits] == ["Fix a typo", "Better jobs page"]


def test_it_refuses_when_something_of_yours_is_in_the_way(repos, tmp_path):
    dev, mine = repos
    publish(dev, **{"app.py": "v = 2\n"})
    (mine / "app.py").write_text("v = 'my edit'\n")
    status = updater.check(mine)
    assert status.available and not status.can_install and "app.py" in status.problem
    sh(mine, "checkout", "--", "app.py")
    (mine / "data").mkdir()
    (mine / "data" / "settings.db").write_text("yours")           # ignored files never block an update
    assert updater.check(mine).can_install
    (mine / "local.py").write_text("x\n")
    sh(mine, "add", "local.py")
    sh(mine, "commit", "--quiet", "-m", "my own commit")
    assert "aren't on origin/main" in updater.check(mine).problem
    assert updater.find_repo(tmp_path / "nowhere" / "x.py") is None


def test_a_container_or_package_install_isnt_a_checkout(monkeypatch):
    monkeypatch.setattr(updater, "find_repo", lambda start=None: None)
    result = updater.check()
    assert not result.available and "not a git checkout" in result.problem and "update-docker.sh" in result.problem


def test_install_fast_forwards_backs_up_and_checks(repos, tmp_path):
    dev, mine = repos
    publish(dev, **{"app.py": "v = 2\n"})
    (mine / "data").mkdir()
    (mine / "data" / "mine.txt").write_text("keep me")
    store = Store(mine / "data" / "jobs.db")
    calls = []
    ok, message = updater.install(mine, store=store, reinstall_fn=lambda r: calls.append("reinstall"),
                                  smoke_fn=lambda r, c: calls.append("check"))
    assert ok and "updated" in message and (mine / "app.py").read_text() == "v = 2\n"
    assert calls == ["reinstall", "check"] and list((mine / "data" / "backups").glob("jobs-*before-update.db"))
    assert (mine / "data" / "mine.txt").read_text() == "keep me"
    assert updater.install(mine, reinstall_fn=lambda r: None, smoke_fn=lambda r, c: None) == (False, "already up to date")


def test_a_failing_new_version_is_rolled_back(repos):
    dev, mine = repos
    old = sh(mine, "rev-parse", "HEAD")
    publish(dev, **{"app.py": "v = 'broken'\n"})
    reinstalls = []

    def broken_check(repo, config):
        raise RuntimeError("config doesn't load")
    ok, message = updater.install(mine, reinstall_fn=lambda r: reinstalls.append(1), smoke_fn=broken_check)
    assert not ok and "rolled back" in message and "config doesn't load" in message
    assert sh(mine, "rev-parse", "HEAD") == old and (mine / "app.py").read_text() == "v = 1\n"
    assert len(reinstalls) == 2                                      # the new version, then the old one again


def test_older_installs_move_their_files_into_data_first(repos):
    dev, mine = repos
    publish(dev, "stories tracked", **{"stories/mine.md": "my story\n"})
    sh(mine, "pull", "--quiet")
    sh(dev, "rm", "--quiet", "-r", "stories")
    publish(dev, "stop tracking personal files", **{
        "tools/migrate_user_data.sh": "mkdir -p data/stories && cp stories/*.md data/stories/\n"})
    ok, message = updater.install(mine, reinstall_fn=lambda r: None, smoke_fn=lambda r, c: None)
    assert ok and (mine / "data" / "stories" / "mine.md").read_text() == "my story\n"
    assert not (mine / "stories" / "mine.md").exists()              # the pull removed it; the copy is safe


def status(**kw):
    values = dict(repo="/x", upstream="origin/main", current="aaa1111", latest="bbb2222", behind=2,
                  commits=["bbb2222 Better jobs page", "ccc3333 Fix"], checked_at="2026-11-26T08:00:00+00:00")
    return UpdateStatus(**{**values, **kw})


def test_notify_mode_pushes_once_per_version(env, monkeypatch):
    fake, runner, board, store, notifier = env
    monkeypatch.setattr(updater, "check", lambda *a, **k: status())
    run(runner.check_updates())
    run(runner.check_updates())
    pushes = [m for m in notifier.sent if m["title"].startswith("Job alerts update")]
    assert len(pushes) == 1 and "Better jobs page" in pushes[0]["message"] and "Settings → Updates" in pushes[0]["message"]
    assert runner.update_status().behind == 2
    assert "Update available: 2 change(s)" in run(runner.send_digest(mark=False))
    runner.cfg.update.mode = "off"
    monkeypatch.setattr(updater, "check", lambda *a, **k: status(latest="ddd4444"))
    run(runner.check_updates())
    assert len([m for m in notifier.sent if m["title"].startswith("Job alerts update")]) == 1


def test_install_mode_installs_and_restarts(env, monkeypatch):
    fake, runner, board, store, notifier = env
    runner.cfg.update.mode = "install"
    monkeypatch.setattr(updater, "check", lambda *a, **k: status())
    monkeypatch.setattr(updater, "install", lambda *a, **k: (True, "updated aaa1111 → bbb2222 (2 changes)"))

    async def go():
        runner.running = True
        runner._main_task = asyncio.current_task()
        try:
            await runner.check_updates()
            await asyncio.sleep(2)
        except asyncio.CancelledError:
            return "restarted"
    import asyncio
    assert run(go()) == "restarted" and runner.restart_requested
    assert notifier.sent[-1]["title"] == "Job alerts updated"
    runner.restart_requested = False
    monkeypatch.setattr(updater, "check", lambda *a, **k: status(problem="shipped files were changed here (x)"))
    store.delete_meta("update.notified")
    run(runner.check_updates())
    assert not runner.restart_requested and "Can't install it on its own" in notifier.sent[-1]["message"]


def test_updates_page(env, monkeypatch):
    from jobalerts.web.app import create_app
    fake, runner, board, store, notifier = env
    monkeypatch.setattr(updater, "check", lambda *a, **k: status())
    installs = []

    async def fake_install():
        installs.append(1)
        return True, "updated aaa1111 → bbb2222 (2 changes)"
    monkeypatch.setattr(runner, "install_update", fake_install)
    app = create_app(runner, b"w" * 32)
    client = TestClient(app, follow_redirects=False)
    token = re.search(r'name="csrf" value="([^"]+)"', client.get("/setup").text).group(1)
    client.post("/setup", data={"csrf": token, "code": app.state.web.setup_code, "password": "update page pw!",
                                "again": "update page pw!"})
    assert TestClient(app, follow_redirects=False).get("/settings/updates").status_code == 303   # needs a login
    assert "Not checked yet" in client.get("/settings/updates").text
    token = re.search(r'name="csrf" value="([^"]+)"', client.get("/settings/updates").text).group(1)
    assert client.post("/settings/updates/check").status_code == 403
    client.post("/settings/updates/check", data={"csrf": token})
    html = client.get("/settings/updates").text
    assert "2 new changes" in html and "Better jobs page" in html and "Update now" in html
    assert client.post("/settings/updates/install").status_code == 403 and not installs
    assert "Restart jobalerts" in client.post("/settings/updates/install", data={"csrf": token}).text and installs
    r = client.post("/settings/updates", data={"csrf": token, "update.mode": "install", "update.interval_hours": "24"})
    assert r.status_code == 303 and runner.cfg.update.mode == "install"
