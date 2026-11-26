"""Web UI foundation: setup code, login, sessions, CSRF, the map share link and security headers."""
import re

import pytest
from fastapi.testclient import TestClient

from jobalerts.config import load_config
from jobalerts.http import HttpClient
from jobalerts.notify import Notifier
from jobalerts.scheduler import Runner
from jobalerts.store import Store
from jobalerts.web import auth
from jobalerts.web.app import create_app, safe_next, script_hashes

from conftest import REPO, run, with_reference_home

KEY = b"k" * 32
PASSWORD = "correct horse battery"


@pytest.fixture(scope="module")
def runner():
    cfg = with_reference_home(load_config(REPO / "config" / "config.yaml"))
    return Runner(cfg, Store(":memory:"), HttpClient(), Notifier(cfg.notify, dry_run=True))


@pytest.fixture
def app(runner):
    runner.store.db.execute("DELETE FROM meta")  # each test starts with no password
    return create_app(runner, KEY)


def csrf_of(client, path="/login"):
    html = client.get(path).text
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def set_up(app) -> TestClient:
    client = TestClient(app, follow_redirects=False)
    token = csrf_of(client, "/setup")
    r = client.post("/setup", data={"csrf": token, "code": app.state.web.setup_code, "password": PASSWORD,
                                    "again": PASSWORD})
    assert r.status_code == 303 and r.headers["location"] == "/"
    return client


def test_first_run_needs_the_setup_code(app, caplog):
    client = TestClient(app, follow_redirects=False)
    assert client.get("/").headers["location"] == "/setup"
    assert client.get("/login").headers["location"] == "/setup"
    code = app.state.web.setup_code
    assert re.fullmatch(r"[0-9A-F]{4}-[0-9A-F]{4}", code)
    token = csrf_of(client, "/setup")
    r = client.post("/setup", data={"csrf": token, "code": "0000-0000", "password": PASSWORD, "again": PASSWORD})
    assert "not the setup code" in r.text
    r = client.post("/setup", data={"csrf": token, "code": code.lower(), "password": "short", "again": "short"})
    assert "at least 10" in r.text
    r = client.post("/setup", data={"csrf": token, "code": code, "password": PASSWORD, "again": PASSWORD})
    assert r.status_code == 303 and client.get("/").status_code == 200
    assert client.get("/setup").headers["location"] == "/login"   # can't be claimed again
    assert app.state.web.setup_code == ""


def test_posts_without_the_csrf_token_are_refused(app):
    client = TestClient(app, follow_redirects=False)
    r = client.post("/setup", data={"code": app.state.web.setup_code, "password": PASSWORD, "again": PASSWORD})
    assert r.status_code == 403 and not app.state.web.password_hash()
    client = set_up(app)
    assert client.post("/settings/account/share-token").status_code == 403
    assert client.post("/settings/account/share-token", data={"csrf": "wrong"}).status_code == 403
    good = csrf_of(client, "/settings/account")
    assert client.post("/settings/account/share-token", headers={"X-CSRF-Token": good}).status_code == 303


def test_login_logout_and_the_next_page(app):
    set_up(app)
    client = TestClient(app, follow_redirects=False)
    r = client.get("/boards?x=1")
    assert r.status_code == 303 and r.headers["location"] == "/login?next=%2Fboards%3Fx%3D1"
    assert client.get("/boards", headers={"HX-Request": "true"}).status_code == 401
    token = csrf_of(client)
    assert "Wrong password" in client.post("/login", data={"csrf": token, "password": "nope"}).text
    r = client.post("/login", data={"csrf": token, "password": PASSWORD, "next": "/boards?x=1"})
    assert r.status_code == 303 and r.headers["location"] == "/boards?x=1"
    assert client.get("/boards").status_code == 200
    assert csrf_of(client, "/settings/account") != token     # a new CSRF token after login
    client.post("/logout", data={"csrf": csrf_of(client, "/settings/account")})
    assert client.get("/").status_code == 303


def test_wrong_passwords_back_off():
    limiter = auth.LoginLimiter()
    for _ in range(4):
        limiter.failed("1.2.3.4", now=1000)
    assert limiter.wait("1.2.3.4", now=1000) == 0
    limiter.failed("1.2.3.4", now=1000)
    assert limiter.wait("1.2.3.4", now=1000) == 30 and limiter.wait("5.6.7.8", now=1000) == 0
    limiter.failed("1.2.3.4", now=1031)
    assert limiter.wait("1.2.3.4", now=1031) == 60
    limiter.succeeded("1.2.3.4")
    assert limiter.wait("1.2.3.4", now=1031) == 0


def test_login_is_rate_limited(app):
    set_up(app)
    client = TestClient(app, follow_redirects=False)
    token = csrf_of(client)
    for _ in range(5):
        client.post("/login", data={"csrf": token, "password": "nope"})
    r = client.post("/login", data={"csrf": token, "password": PASSWORD})
    assert "Too many tries" in r.text and client.get("/").status_code == 303


def test_only_same_site_next_pages():
    for bad in ("//evil.example", "https://evil.example", "/\\evil.example", "", None, "jobs"):
        assert safe_next(bad) == "/"
    assert safe_next("/jobs/abc?x=1") == "/jobs/abc?x=1"


def test_a_tampered_or_old_session_is_not_logged_in(app):
    client = set_up(app)
    cookie = client.cookies[auth.COOKIE]
    body, sig = cookie.split(".")
    other_sig = auth.Signer(b"z" * 32).dumps({"x": 1}).split(".")[1]   # a valid-looking signature, wrong key
    forged = TestClient(app, follow_redirects=False, cookies={auth.COOKIE: body + "." + other_sig})
    assert forged.get("/").status_code == 303
    other = TestClient(app, follow_redirects=False, cookies={auth.COOKIE: cookie})
    assert other.get("/").status_code == 200
    # changing the password logs every other session out, but not this one
    token = csrf_of(client, "/settings/account")
    r = client.post("/settings/account/password", data={"csrf": token, "current": PASSWORD,
                                                        "password": "another long one", "again": "another long one"})
    assert r.status_code == 303 and client.get("/").status_code == 200
    assert other.get("/").status_code == 303


def test_the_share_link_is_off_until_turned_on_and_opens_only_the_shared_map(app):
    client = set_up(app)
    guest = TestClient(app, follow_redirects=False)
    assert app.state.web.share_token() == "" and "The link is off" in client.get("/settings/account").text
    assert guest.get("/map/embed?token=").status_code == 404 and guest.get("/map/embed?token=x").status_code == 404
    client.post("/settings/account/share-token", data={"csrf": csrf_of(client, "/settings/account")})
    share = app.state.web.share_token()
    assert share and "/map/embed?token=" + share in client.get("/settings/account").text
    r = guest.get(f"/map/embed?token={share}")
    assert r.status_code == 200 and "const D = {" in r.text and '"private":true' in r.text
    assert "frame-ancestors *" in r.headers["content-security-policy"]
    assert r.headers["referrer-policy"] == "no-referrer"
    assert guest.get("/map/embed?token=nope").status_code == 404
    assert guest.get("/map/embed").status_code == 404
    assert guest.get(f"/settings/account?token={share}").status_code == 303
    client.post("/settings/account/share-token", data={"csrf": csrf_of(client, "/settings/account")})
    assert guest.get(f"/map/embed?token={share}").status_code == 404   # the old link stops working
    client.post("/settings/account/share-off", data={"csrf": csrf_of(client, "/settings/account")})
    assert guest.get(f"/map/embed?token={app.state.web.share_token()}").status_code == 404
    assert "The link is off" in client.get("/settings/account").text


def test_the_shared_map_and_pay_notes_carry_none_of_your_figures(app, runner):
    import json as _json
    from jobalerts import col, colmap
    client = set_up(app)
    private = client.get("/map/embed").text                      # logged in: the full map
    assert "$3,400 payment" in private                             # the test household's mortgage
    client.post("/settings/account/share-token", data={"csrf": csrf_of(client, "/settings/account")})
    shared = TestClient(app).get(f"/map/embed?token={app.state.web.share_token()}").text
    for figure in ("3,400", "$750", "4.5%", "Portland-Vancouver-Hillsboro, OR-WA metro (RPP"):
        assert figure not in shared, figure
    data = _json.loads(re.search(r"const D = (\{.*?\});\n", shared, re.S).group(1).replace("<\\/", "</"))
    assert data["reference"]["private"] and data["reference"]["label"] == colmap._short(runner.pay.home.metro)
    note = runner.pay.index(col.Pay(155_000, 155_000, "USD"), "Portland, OR").text()   # alerts, digest, HA
    assert "staying in your home" in note and "3,400" not in note and "$750" not in note
    assert "3,400" not in runner.pay.home.label


def test_logged_in_map_page_and_its_script_hash(app, runner):
    client = set_up(app)
    assert 'src="/map/embed"' in client.get("/map").text
    r = client.get("/map/embed")
    csp = r.headers["content-security-policy"]
    assert "frame-ancestors 'self'" in csp and script_hashes(r.text) in csp and "'unsafe-inline'" not in csp.split("style-src")[0]
    assert r.text == run(runner.current_map())


def test_security_headers_and_cookie_flags(app):
    client = TestClient(app, follow_redirects=False)
    r = client.get("/setup")
    assert r.headers["content-security-policy"].startswith("default-src 'self'")
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["cache-control"] == "no-store"
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie and "secure" not in cookie
    assert client.get("/healthz").text == "ok\n"
    assert client.get("/static/htmx.min.js").status_code == 200


def test_unknown_pages_are_404_and_every_nav_page_exists(app):
    client = set_up(app)
    assert client.get("/nope").status_code == 404
    for path in ("/", "/boards", "/map", "/stories", "/settings/situation"):
        assert client.get(path).status_code == 200, path


def test_signer_and_passwords():
    signer = auth.Signer(KEY)
    token = signer.dumps({"a": 1})
    assert signer.loads(token) == {"a": 1}
    assert auth.Signer(b"x" * 32).loads(token) is None and signer.loads("garbage") is None
    stored = auth.hash_password("a long password")
    assert auth.check_password("a long password", stored) and not auth.check_password("wrong", stored)
    assert not auth.check_password("x", "md5$abc")


def test_key_file_is_created_private_and_reused(tmp_path):
    key = auth.load_or_create_key(tmp_path / "d" / "secret.key")
    assert len(key) == 32 and (tmp_path / "d" / "secret.key").stat().st_mode & 0o777 == 0o600
    assert auth.load_or_create_key(tmp_path / "d" / "secret.key") == key


def test_set_password_command(tmp_path, monkeypatch, capsys):
    from jobalerts import cli
    from jobalerts.config import Config
    answers = iter([PASSWORD, PASSWORD])
    monkeypatch.setattr("getpass.getpass", lambda prompt="": next(answers))
    cfg = Config(database=str(tmp_path / "x.db"))
    assert run(cli.cmd_set_password(cfg, None)) == 0
    store = Store(tmp_path / "x.db")
    assert auth.check_password(PASSWORD, store.get_meta("web.password_hash")) and store.get_meta("web.password_ver") == "1"


def test_the_service_starts_the_web_ui(runner, monkeypatch):
    served = []

    async def fake_serve(r):
        served.append(r)

    async def nothing(*a, **k):
        return None
    monkeypatch.setattr("jobalerts.web.app.serve", fake_serve)
    for name in ("_board_loop", "_daily_loop", "_dashboard_loop", "_heartbeat_loop", "_update_loop",
                 "refresh_pay_data"):
        monkeypatch.setattr(runner, name, nothing)
    monkeypatch.setattr(runner.cfg, "companies", runner.cfg.companies[:1])
    run(runner.run_forever())
    assert served == [runner]
