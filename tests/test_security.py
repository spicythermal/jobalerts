"""Security checks: no API key ever leaves the backend (pages, errors, MQTT, map, logs, database file),
the web package reads keys only to test them with their provider, and job text is escaped."""
import ast
import logging
import re
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from jobalerts import vault
from jobalerts.config import load_config
from jobalerts.dashboard import MqttPublisher
from jobalerts.http import HttpClient
from jobalerts.notify import Notifier
from jobalerts.scheduler import Runner
from jobalerts.sources import SOURCES
from jobalerts.store import Store
from jobalerts.web.app import create_app
from jobalerts.web.settings_pages import SUBNAV

from conftest import REPO, run, with_reference_home
from test_pipeline import make_job

PASSWORD = "security test password"
CANARIES = {name: f"canary{i:02d}-{name.lower()}-7f3a9c" for i, name in enumerate(vault.SECRETS)}
SHOWN = {name for name, (_, shown) in vault.SECRETS.items() if shown}   # not secret (an email address)


@pytest.fixture
def env(tmp_path, monkeypatch):
    for name in vault.SECRETS:
        monkeypatch.delenv(name, raising=False)
    cfg = with_reference_home(load_config(REPO / "config" / "config.yaml"))
    cfg.config_dir = tmp_path / "config"
    for name in ("col_data", "tax_rules", "tax_data"):
        setattr(cfg.pay, name, str(REPO / "config" / getattr(cfg.pay, name)))
    store = Store(tmp_path / "data" / "jobs.db")
    sent = []
    runner = Runner(cfg, store, HttpClient(), Notifier(cfg.notify, dry_run=True),
                    homeassistant=MqttPublisher(cfg.homeassistant, send=lambda c, messages: sent.extend(messages)))
    app = create_app(runner, b"q" * 32)
    client = TestClient(app, follow_redirects=False)
    token = re.search(r'name="csrf" value="([^"]+)"', client.get("/setup").text).group(1)
    client.post("/setup", data={"csrf": token, "code": app.state.web.setup_code, "password": PASSWORD, "again": PASSWORD})
    token = re.search(r'name="csrf" value="([^"]+)"', client.get("/settings/keys").text).group(1)
    for name, value in CANARIES.items():
        assert client.post(f"/settings/keys/{name}", data={"csrf": token, "value": value}).status_code == 303
    vault.install(runner.vault)
    yield runner, app, client, token, sent, tmp_path
    vault.install(None)
    store.close()


def leaked(text: str) -> list[str]:
    return [n for n, v in CANARIES.items() if v in text and n not in SHOWN]


def get_paths(app) -> list[str]:
    paths = []
    for route in app.routes:
        methods = getattr(route, "methods", None) or set()
        if "GET" not in methods:
            continue
        path = route.path
        if path == "/settings/{section}":
            paths += [f"/settings/{key}" for key, _ in SUBNAV]
        elif "{" not in path:
            paths.append(path)
    return paths + ["/nope", "/jobs/../../etc/passwd", "/settings/nope", "/static/../../secret.key"]


def test_no_page_shows_a_secret(env):
    runner, app, client, token, _, _ = env
    runner.store.insert_job(make_job(1, title="Plasma Physicist"), "alerted")
    paths = get_paths(app)
    assert "/settings/keys" in paths and "/map/embed" in paths and len(paths) > 12
    for path in paths:
        r = client.get(path)
        assert not leaked(r.text) and not leaked(str(r.headers)), path
    # failed form posts re-render pages: none echoes a stored key either
    for path, data in (("/settings/keys/NTFY_TOKEN", {"value": " "}), ("/settings/situation", {"pay.reference_salary": "x"}),
                       ("/settings/alerts", {"scoring.push_threshold": "x"}), ("/login", {"password": "x"})):
        r = client.post(path, data={"csrf": token, **data})
        assert not leaked(r.text), path
    for name in CANARIES:
        if name in ("ANTHROPIC_API_KEY", "USAJOBS_API_KEY", "USAJOBS_EMAIL", "NTFY_TOPIC", "NTFY_TOKEN", "MQTT_PASSWORD"):
            app.state.web.test_transport = httpx.MockTransport(
                lambda request: httpx.Response(401, text=f"bad key {request.headers.get('x-api-key')} "
                                                         f"{request.headers.get('authorization-key')}"))
            r = client.post(f"/settings/keys/{name}/test", headers={"X-CSRF-Token": token})
            assert not leaked(r.text), name


def test_published_surfaces_and_the_database_never_hold_secrets(env):
    runner, app, client, token, sent, tmp_path = env
    run(runner.publish_dashboard())
    assert sent and not leaked(repr(sent))
    assert not leaked(run(runner.current_map()))
    runner.store.db.execute("PRAGMA wal_checkpoint(FULL)")
    raw = b"".join(p.read_bytes() for p in (tmp_path / "data").glob("jobs.db*")).decode("latin-1")
    assert not leaked(raw) and not [n for n, v in CANARIES.items() if v in raw]   # shown ones are encrypted too


def test_logs_never_show_a_secret(env, caplog):
    runner, *_ = env
    vault.install_log_redaction()    # the service attaches the filter to every root handler, caplog's included
    caplog.set_level(logging.DEBUG)

    def handler(request):
        return httpx.Response(500, text="upstream exploded")
    http = HttpClient(host_min_interval={"default": 0.0}, transport=httpx.MockTransport(handler))
    from jobalerts.config import Board
    board = Board("adzuna", "physicist", "adzuna 'physicist'", 3600, {"what": "physicist", "country": "us"}, watched=False)
    with pytest.raises(httpx.HTTPStatusError) as e:
        run(SOURCES["adzuna"].fetch(board, http))
    assert CANARIES["ADZUNA_APP_KEY"] in str(e.value)    # the raw error carries the key in its URL...
    logging.getLogger("jobalerts.scheduler").warning("adzuna:physicist failed: %s", e.value)
    logging.getLogger("jobalerts.scheduler").exception("boom", exc_info=e.value)
    assert not leaked(caplog.text) and "app_key=[redacted]" in caplog.text   # ...the log doesn't


def test_the_web_package_reads_keys_only_to_test_them():
    web = Path(__file__).parent.parent / "src" / "jobalerts" / "web"
    for path in web.glob("*.py"):
        source = path.read_text()
        assert "os.environ" not in source and "vault.get(" not in source and ".get(name" not in source, path
        tree = ast.parse(source)
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            calls = {n.func.id for n in ast.walk(fn) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
            if "secret" in calls:   # sends the key to its own provider to check it; the answer is redacted
                assert (path.name, fn.name) in {("settings_pages.py", "_anthropic"), ("settings_pages.py", "_usajobs")}


def test_job_text_is_escaped(env):
    runner, app, client, *_ = env
    job = make_job(7, title='<script>alert("x")</script> Physicist', company="<img src=x onerror=alert(1)>")
    runner.store.insert_job(job, "alerted")
    runner.store.update_job(job.key, status="alerted", score=90, score_summary="<b>bold</b>")
    evil = make_job(8, title="Link Physicist")
    evil.url = "javascript:alert(document.cookie)"
    runner.store.insert_job(evil, "alerted")
    runner.store.update_job(evil.key, status="alerted", score=91)
    html = client.get("/").text
    assert "<script>alert" not in html and "&lt;script&gt;" in html and "<img src=x" not in html
    assert "javascript:" not in html            # a posting's link is only ever http(s)
