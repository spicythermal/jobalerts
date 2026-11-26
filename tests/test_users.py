"""Accounts: the first admin, roles, one-time passwords, the upgrade from one password, the admin-only
pages, each user's own pipeline, and which Anthropic key each user's AI calls use."""
import re
import sqlite3
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from jobalerts import desktop, sources, vault
from jobalerts.config import Board, Config
from jobalerts.http import HttpClient
from jobalerts.models import utcnow
from jobalerts.notify import Notifier
from jobalerts.scheduler import Runner
from jobalerts.settings import UiSettings
from jobalerts.store import Store
from jobalerts.users import OWNER, UserError, Users, one_time_password
from jobalerts.web import auth
from jobalerts.web.app import admin_only, create_app

from test_pipeline import FakeScorer, FakeSource, make_job

PASSWORD = "correct horse battery"


def csrf_of(client, path="/login"):
    return re.search(r'name="csrf" value="([^"]+)"', client.get(path).text).group(1)


def test_accounts_and_their_rules(tmp_path):
    store = Store(tmp_path / "jobs.db")
    users = Users(store)
    with pytest.raises(UserError):
        users.create("No Spaces", PASSWORD)
    owner = users.create("Admin", PASSWORD, role="user")              # the first account is the admin, id 1
    assert (owner.id, owner.username, owner.role, owner.must_change) == (OWNER, "admin", "admin", False)
    alex = users.create("alex", one_time_password())
    assert alex.id == 2 and alex.role == "user" and alex.must_change
    with pytest.raises(UserError, match="already"):
        users.create("ALEX", PASSWORD)
    assert users.authenticate("admin", PASSWORD).id == OWNER
    assert users.authenticate("admin", "wrong") is None and users.authenticate("nobody", PASSWORD) is None
    for change in (lambda: users.set_role(OWNER, "user"), lambda: users.set_disabled(OWNER, True),
                   lambda: users.delete(OWNER)):
        with pytest.raises(UserError, match="first admin"):
            change()
    users.set_role(alex.id, "admin")
    assert users.get(alex.id).admin and users.get(alex.id).ver == alex.ver + 1      # logs alex's sessions out
    users.set_disabled(alex.id, True)
    assert users.authenticate("alex", PASSWORD) is None
    folder = tmp_path / "users" / "2"
    folder.mkdir(parents=True)
    (folder / "profile.md").write_text("alex's profile")
    store.user(2).insert_job(make_job(1), "alerted")
    users.delete(alex.id, tmp_path)
    assert users.get(alex.id) is None and not folder.exists()
    assert list((tmp_path / "users" / ".trash").iterdir())[0].name.startswith("2-alex-")
    assert store.db.execute("SELECT COUNT(*) FROM user_jobs WHERE user_id=2").fetchone()[0] == 0


def test_the_last_enabled_admin_stays(tmp_path):
    users = Users(Store(tmp_path / "jobs.db"))
    users.create("admin", PASSWORD)
    second = users.create("sam", PASSWORD, role="admin")
    users.set_disabled(OWNER, False)
    users.set_role(second.id, "user")                                # user 1 is still an admin
    users.set_role(second.id, "admin")
    assert users.enabled_admins() == 2


def old_install(path):
    """A database as the single-password version left it."""
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL, encrypted INTEGER NOT NULL DEFAULT 0,
                               updated_at TEXT NOT NULL);
        CREATE TABLE secrets (name TEXT PRIMARY KEY, ciphertext TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE ai_usage (day TEXT PRIMARY KEY, calls INTEGER NOT NULL DEFAULT 0,
                               input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
                               cache_read_tokens INTEGER NOT NULL DEFAULT 0, cache_write_tokens INTEGER NOT NULL DEFAULT 0);
    """)
    now = utcnow().isoformat()
    db.execute("INSERT INTO meta VALUES ('web.password_hash', ?, ?)", (auth.hash_password(PASSWORD), now))
    db.execute("INSERT INTO meta VALUES ('web.password_ver', '3', ?)", (now,))
    db.execute("INSERT INTO settings VALUES ('scoring.push_threshold', '81', 0, ?)", (now,))
    db.execute("INSERT INTO settings VALUES ('update.mode', '\"install\"', 0, ?)", (now,))
    fernet = vault.fernet_for(b"m" * 32)
    for name, value in (("NTFY_TOPIC", "owner-topic-123"), ("ANTHROPIC_API_KEY", "sk-install-123456")):
        db.execute("INSERT INTO secrets VALUES (?, ?, ?)", (name, fernet.encrypt(value.encode()).decode(), now))
    db.execute("INSERT INTO ai_usage (day, calls) VALUES (?, 7)", (utcnow().date().isoformat(),))
    db.execute("PRAGMA user_version = 2")
    db.commit()
    db.close()


def test_an_older_install_becomes_user_1(tmp_path, monkeypatch):
    for name in vault.SECRETS:
        monkeypatch.delenv(name, raising=False)
    path = tmp_path / "jobs.db"
    Store(path).close()          # the tables of today, then the old ones' contents
    db = sqlite3.connect(path)
    db.executescript("DROP TABLE users; DROP TABLE user_jobs; DROP TABLE ai_calls; DROP TABLE meta; DROP TABLE ai_usage;")
    db.close()
    old_install(path)
    db = sqlite3.connect(path)
    job = make_job(1)
    db.execute("INSERT INTO jobs (key, source, board, job_id, company, title, url, first_seen_at, status, score, "
               "user_state) VALUES (?, 'fake', 'acme', '1', 'Acme', 'Network Engineer', 'https://x/1', ?, 'alerted', "
               "88, 'saved')", (job.key, utcnow().isoformat()))
    db.commit()
    db.close()
    store = Store(path)
    owner = Users(store).authenticate("admin", PASSWORD)              # the old password, username "admin"
    assert owner.id == OWNER and owner.admin
    row = store.get_job(job.key)
    assert (row["status"], row["score"], row["user_state"]) == ("alerted", 88, "saved")
    assert store.user(2).get_job(job.key)["status"] == "seen" and store.user(2).get_job(job.key)["score"] is None
    assert store.ai_calls_today() == 7
    v = vault.Vault(store, b"m" * 32)
    assert v.get("NTFY_TOPIC", OWNER) == "owner-topic-123" and v.get("NTFY_TOPIC", 2) == ""
    assert v.get("ANTHROPIC_API_KEY") == "sk-install-123456"           # the install's
    ui = UiSettings(store, v)
    assert ui.values() == {"scoring.push_threshold": 81, "update.mode": "install"}
    assert UiSettings(store, v, 2).values() == {"update.mode": "install"}  # the install's, not user 1's threshold
    assert list((tmp_path / "backups").glob("jobs-*-before-schema-3.db"))


def two_people(monkeypatch, tmp_path, share=False):
    fake = FakeSource()
    monkeypatch.setitem(sources.SOURCES, "fake", fake)
    monkeypatch.setattr("jobalerts.scheduler.SOURCES", sources.SOURCES)
    for name in vault.SECRETS:
        monkeypatch.delenv(name, raising=False)
    cfg = Config.model_validate({
        "criteria": {"title_include": ["network engineer"], "allow_remote": True},
        "companies": [{"name": "Acme", "ats": "greenhouse", "slug": "acme"}],
        "scoring": {"profile_path": str(tmp_path / "data" / "profile.md")},
        "notify": {"topic": "owner-topic"},
    })
    cfg.config_dir = tmp_path / "config"
    store = Store(tmp_path / "data" / "jobs.db")
    runner = Runner(cfg, store, HttpClient(), Notifier(cfg.notify, dry_run=True))
    runner.vault = vault.Vault(store, b"p" * 32)
    runner.ui_settings = UiSettings(store, runner.vault)
    runner.users = Users(store)
    runner.users.create("admin", PASSWORD)
    alex = runner.users.create("alex", PASSWORD, must_change=False)
    if share:
        runner.ui_settings.save({"users.share_keys": True})
    runner.settings_for(alex.id).save({"criteria.title_include": ["data scientist"], "notify.digest_time": "07:30"})
    runner.vault.set("NTFY_TOPIC", "alex-topic", alex.id)
    runner.sync_people()
    return fake, runner, alex


def test_each_user_gets_their_own_decisions(monkeypatch, tmp_path):
    fake, runner, alex = two_people(monkeypatch, tmp_path)
    board = Board("fake", "acme", "Acme", 600)
    run_poll = __import__("conftest").run
    run_poll(runner.poll_board(board))                                  # baseline
    fake.jobs = [make_job(1, title="Network Engineer"), make_job(2, title="Data Scientist")]
    runner.me.scorer = FakeScorer({"Network Engineer": 90})
    runner.people[alex.id].scorer = FakeScorer({"Data Scientist": 85})
    run_poll(runner.poll_board(board))
    mine, theirs = runner.store, runner.store.user(alex.id)
    assert (mine.get_job("fake:acme:1")["status"], mine.get_job("fake:acme:2")["status"]) == ("alerted", "filtered")
    assert (theirs.get_job("fake:acme:1")["status"], theirs.get_job("fake:acme:2")["status"]) == ("filtered", "alerted")
    assert fake.enriched.count("fake:acme:2") == 1                       # fetched once, for whoever needed it
    assert [m["topic"] for m in runner.me.notifier.sent] == ["owner-topic"]
    alex_person = runner.people[alex.id]
    assert [m["topic"] for m in alex_person.notifier.sent] == ["alex-topic"]
    assert alex_person.cfg.notify.digest_time == "07:30" and runner.cfg.notify.digest_time != "07:30"
    # Saving is per user too
    theirs.set_user_state("fake:acme:1", "saved")
    assert mine.get_job("fake:acme:1")["user_state"] is None


def test_a_new_user_starts_from_the_shipped_defaults(monkeypatch, tmp_path):
    fake, runner, alex = two_people(monkeypatch, tmp_path)
    runner.base_cfg.pay.reference_salary = 222_222        # e.g. from user 1's config.local.yaml
    runner.base_cfg.value_sources = {"pay.reference_salary": "local", "polling.jitter": "local"}
    base = runner.personal_base(alex.id)
    assert base.pay.reference_salary != 222_222 and base.notify.topic == ""
    assert base.scoring.profile_path.endswith("users/2/profile.md".replace("/", __import__("os").sep))
    assert base.value_sources == {"polling.jitter": "local"}
    assert (tmp_path / "data" / "users" / "2" / "profile.md").is_file()    # seeded from the example
    assert (tmp_path / "data" / "users" / "2" / "stories").is_dir()


def test_which_key_each_user_scores_with(monkeypatch, tmp_path):
    fake, runner, alex = two_people(monkeypatch, tmp_path)
    v = runner.vault
    v.set("ANTHROPIC_API_KEY", "sk-install-aaaaaa")
    assert v.anthropic_key(OWNER, admin=True, share=False) == ("sk-install-aaaaaa", False)
    assert v.anthropic_key(alex.id, admin=False, share=False) == ("", False)             # not shared: none
    assert v.anthropic_key(alex.id, admin=False, share=True) == ("sk-install-aaaaaa", True)
    v.set("ANTHROPIC_API_KEY", "sk-alex-bbbbbb", alex.id)
    assert v.anthropic_key(alex.id, admin=False, share=True) == ("sk-alex-bbbbbb", False)  # their own wins
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-env-cccccc")                               # the owner's .env
    v.clear("ANTHROPIC_API_KEY", alex.id)
    assert v.anthropic_key(alex.id, admin=False, share=False) == ("", False)
    assert v.get("ANTHROPIC_API_KEY", alex.id) == ""                                       # .env isn't theirs


def test_no_key_of_their_own_means_no_ai_scoring(monkeypatch, tmp_path):
    from jobalerts import cli
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-env-cccccc")
    cfg = Config.model_validate({"scoring": {"enabled": True, "profile_path": str(tmp_path / "p.md")}})
    (tmp_path / "p.md").write_text("A physicist.")
    store = Store(":memory:").user(2)
    assert cli.make_scorer(cfg, store, api_key="") is None                # never the environment's key
    scorer = cli.make_scorer(cfg, store, api_key="sk-own-dddddd")
    assert scorer is not None and scorer.client.api_key == "sk-own-dddddd"


def test_the_shared_key_has_its_own_daily_cap(tmp_path):
    store = Store(tmp_path / "jobs.db")
    alex, sam = store.user(2), store.user(3)
    for view in (alex, sam):
        view.shared_key, view.shared_cap = True, 3
    alex.record_ai_call(10, 10)
    alex.record_ai_call(10, 10)
    assert alex.ai_calls_left(100) == 1 and sam.ai_calls_left(100) == 1   # the cap is for everyone together
    sam.record_ai_call(10, 10)
    assert alex.ai_calls_left(100) == 0
    store.user(1).record_ai_call(10, 10)                                  # an admin's calls don't count
    assert store.shared_calls_today() == 3 and store.ai_calls_today() == 1
    own = store.user(4)
    assert own.ai_calls_left(5) == 5                                        # a user on their own key


# ---------------------------------------------------------------------- the web UI
@pytest.fixture
def web(monkeypatch, tmp_path):
    fake, runner, alex = two_people(monkeypatch, tmp_path)
    runner.users.db.execute("DELETE FROM users WHERE id=?", (alex.id,))
    runner.sync_people()
    app = create_app(runner, b"w" * 32)
    admin = TestClient(app, follow_redirects=False)
    token = csrf_of(admin)
    assert admin.post("/login", data={"csrf": token, "username": "admin", "password": PASSWORD}).status_code == 303
    return runner, app, admin


def add_user(app, admin, name, role="user"):
    """An admin adds `name`; they log in with the one-time password and choose their own."""
    html = admin.post("/settings/users/add", data={"csrf": csrf_of(admin, "/settings/users"), "username": name,
                                                   "role": role}).text
    once = re.search(r'value="([0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4})"', html).group(1)
    client = TestClient(app, follow_redirects=False)
    r = client.post("/login", data={"csrf": csrf_of(client), "username": name, "password": once})
    assert r.headers["location"] == "/settings/account?must=1"
    assert client.get("/").headers["location"] == "/settings/account?must=1"      # nothing else until then
    assert client.post("/filter/save", headers={"X-CSRF-Token": csrf_of(client, "/settings/account")}).status_code == 403
    token = csrf_of(client, "/settings/account")
    r = client.post("/settings/account/password", data={"csrf": token, "current": once, "password": PASSWORD,
                                                        "again": PASSWORD})
    assert r.status_code == 303 and client.get("/").status_code == 200
    return client


def test_admins_add_users_who_choose_their_password(web):
    runner, app, admin = web
    alex = add_user(app, admin, "alex")
    assert "alex" in admin.get("/settings/users").text
    assert runner.users.by_name("alex").id in runner.people                # their pipeline runs
    # a reset gives a new one-time password and logs alex out
    uid = runner.users.by_name("alex").id
    admin.post(f"/settings/users/{uid}/reset", data={"csrf": csrf_of(admin, "/settings/users")})
    assert alex.get("/").status_code == 303
    # disable: no login; delete needs the name typed
    admin.post(f"/settings/users/{uid}/disable", data={"csrf": csrf_of(admin, "/settings/users")})
    assert uid not in runner.people
    html = admin.post(f"/settings/users/{uid}/delete", data={"csrf": csrf_of(admin, "/settings/users"),
                                                             "confirm": "nope"}).text
    assert "Type alex" in html and runner.users.get(uid)
    admin.post(f"/settings/users/{uid}/delete", data={"csrf": csrf_of(admin, "/settings/users"), "confirm": "alex"})
    assert runner.users.get(uid) is None
    html = admin.post(f"/settings/users/{OWNER}/role", data={"csrf": csrf_of(admin, "/settings/users"),
                                                             "role": "user"}).text
    assert "first admin stays an admin" in html


def test_users_cant_open_admin_pages(web):
    runner, app, admin = web
    alex = add_user(app, admin, "alex")
    token = csrf_of(alex, "/settings/account")
    checked = 0
    for route in app.routes:
        for method in getattr(route, "methods", None) or ():
            path = re.sub(r"\{[^}]+\}", "1", route.path)
            if not admin_only(method, path):
                continue
            r = alex.request(method, path, headers={"X-CSRF-Token": token})
            assert r.status_code == 403, (method, path)
            checked += 1
    assert checked >= 15
    for path in ("/settings/users", "/settings/connections", "/settings/updates"):
        assert alex.get(path).status_code == 403 and admin.get(path).status_code == 200
    assert alex.post("/settings/keys/install/ADZUNA_APP_KEY", data={"csrf": token, "value": "x"}).status_code == 403
    assert alex.post("/settings/keys/mine/ADZUNA_APP_KEY", data={"csrf": token, "value": "x"}).status_code == 404
    boards = alex.get("/boards").text
    assert "Find its job board" not in boards and "Ask an admin" in boards
    settings = alex.get("/settings/situation").text
    assert "/settings/users" not in settings and "/settings/connections" not in settings
    keys = alex.get("/settings/keys").text
    assert "This install's (admins)" not in keys and "/settings/keys/mine/ANTHROPIC_API_KEY" in keys
    assert "doesn't share its key" in keys


def test_sharing_the_key_changes_what_users_see(web):
    runner, app, admin = web
    runner.vault.set("ANTHROPIC_API_KEY", "sk-install-aaaaaa")
    alex = add_user(app, admin, "alex")
    person = runner.people[runner.users.by_name("alex").id]
    assert person.key_source == ""
    admin.post("/settings/users", data={"csrf": csrf_of(admin, "/settings/users"), "users.share_keys": "1",
                                        "users.shared_daily_max_calls": "50"})
    assert person.cfg.users.share_keys and person.key_source == "shared"
    assert "this install's key (shared by an admin" in alex.get("/settings/keys").text
    alex.post("/settings/keys/mine/ANTHROPIC_API_KEY", data={"csrf": csrf_of(alex, "/settings/keys"),
                                                             "value": "sk-alex-bbbbbb"})
    assert person.key_source == "own" and runner.vault.get("ANTHROPIC_API_KEY") == "sk-install-aaaaaa"


def test_setup_makes_the_first_admin_and_asks_about_the_key(monkeypatch, tmp_path):
    fake, runner, alex = two_people(monkeypatch, tmp_path)
    runner.store.db.execute("DELETE FROM users")
    runner.store.db.execute("DELETE FROM settings_by_user")
    app = create_app(runner, b"s" * 32)
    client = TestClient(app, follow_redirects=False)
    html = client.get("/setup").text
    assert 'value="admin"' in html and 'name="share"' in html
    token = csrf_of(client, "/setup")
    r = client.post("/setup", data={"csrf": token, "code": app.state.web.setup_code, "username": "bad name",
                                    "password": PASSWORD, "again": PASSWORD, "share": "yes"})
    assert "2 to 32 characters" in r.text and not runner.users.any()
    r = client.post("/setup", data={"csrf": token, "code": app.state.web.setup_code, "username": "Robin",
                                    "password": PASSWORD, "again": PASSWORD, "share": "yes"})
    assert r.status_code == 303 and runner.users.get(OWNER).username == "robin"
    assert runner.cfg.users.share_keys


def test_old_sessions_are_logged_out(web):
    runner, app, admin = web
    cookie = auth.Signer(b"w" * 32).dumps({"csrf": "x", "user": "admin", "ver": 1, "exp": 4_000_000_000})
    old = TestClient(app, follow_redirects=False, cookies={auth.COOKIE: cookie})   # a single-password era cookie
    assert old.get("/").status_code == 303


def test_logging_out_ends_the_session_for_every_copy_of_the_cookie(web):
    runner, app, admin = web
    copy = TestClient(app, follow_redirects=False, cookies={auth.COOKIE: admin.cookies.get(auth.COOKIE)})
    assert copy.get("/").status_code == 200
    admin.post("/logout", data={"csrf": csrf_of(admin, "/settings/account")})
    assert admin.get("/").status_code == 303 and copy.get("/").status_code == 303
    # a login from before session ids gets one, and logging out ends it too
    owner = runner.users.get(OWNER)
    cookie = auth.Signer(b"w" * 32).dumps({"csrf": "x", "uid": OWNER, "ver": owner.ver, "exp": 4_000_000_000})
    r = TestClient(app, follow_redirects=False, cookies={auth.COOKIE: cookie}).get("/")
    assert r.status_code == 200 and auth.Signer(b"w" * 32).loads(r.cookies[auth.COOKIE])["sid"]
    older = TestClient(app, follow_redirects=False, cookies={auth.COOKIE: r.cookies[auth.COOKIE]})
    copy = TestClient(app, follow_redirects=False, cookies={auth.COOKIE: r.cookies[auth.COOKIE]})
    older.post("/logout", data={"csrf": "x"})
    assert copy.get("/").status_code == 303


def form_fields(html: str) -> dict:
    """A settings form's current values, as the browser would send them."""
    data = {}
    for m in re.finditer(r"<input([^>]*)>", html):
        attrs = m.group(1)
        name, value = re.search(r'name="([^"]+)"', attrs), re.search(r'value="([^"]*)"', attrs)
        if name and not ('type="checkbox"' in attrs and "checked" not in attrs):
            data[name.group(1)] = value.group(1) if value else ""
    for m in re.finditer(r'<select name="([^"]+)".*?</select>', html, re.S):
        chosen = re.search(r'<option value="([^"]*)"[^>]*selected', m.group(0))
        data[m.group(1)] = chosen.group(1) if chosen else ""
    return data


def test_only_admins_choose_the_ntfy_server(web):
    runner, app, admin = web
    alex = add_user(app, admin, "alex")
    uid = runner.users.by_name("alex").id
    form = form_fields(alex.get("/settings/alerts").text)
    assert "notify.server" not in form
    alex.post("/settings/alerts", data={**form, "notify.server": "http://127.0.0.1:9/x"})
    assert runner.person(uid).notifier.cfg.server == "https://ntfy.sh"
    assert alex.post("/settings/connections", data={**form, "notify.server": "http://127.0.0.1:9/x"}).status_code == 403
    form = form_fields(admin.get("/settings/connections").text)
    r = admin.post("/settings/connections", data={**form, "notify.server": "file:///etc/passwd"})
    assert r.status_code == 200 and runner.person(uid).notifier.cfg.server == "https://ntfy.sh"
    r = admin.post("/settings/connections", data={**form, "notify.server": "https://ntfy.example.org"})
    assert r.status_code == 303
    assert {p.notifier.cfg.server for p in runner.everyone()} == {"https://ntfy.example.org"}


def test_a_users_own_ntfy_server_becomes_the_installs(tmp_path, caplog):
    store = Store(tmp_path / "jobs.db")
    v = vault.Vault(store, b"n" * 32)
    UiSettings(store, v)   # the table
    for uid, server in ((1, "https://ntfy.example.org"), (2, "http://10.0.0.5")):
        store.db.execute("INSERT INTO settings_by_user (user_id, key, value, encrypted, updated_at) "
                         "VALUES (?, 'notify.server', ?, 0, '2026-01-01')", (uid, f'"{server}"'))
    store.delete_meta("settings.moved.notify.server")
    UiSettings(store, v)
    assert "user 2's own value is no longer used" in caplog.text
    for uid in (1, 2, 3):
        assert UiSettings(store, v, uid).values() == {"notify.server": "https://ntfy.example.org"}


def test_a_failed_key_test_shows_no_details(web):
    import httpx
    runner, app, admin = web
    alex = add_user(app, admin, "alex")

    def refuse(request):
        raise httpx.ConnectError("connecting to 10.1.2.3:8080 refused")
    app.state.web.test_transport = httpx.MockTransport(refuse)
    alex.post("/settings/keys/mine/ANTHROPIC_API_KEY", data={"csrf": csrf_of(alex, "/settings/keys"),
                                                             "value": "sk-test-key-123"})
    html = alex.post("/settings/keys/mine/ANTHROPIC_API_KEY/test", data={"csrf": csrf_of(alex, "/settings/keys")}).text
    assert "The log has the details" in html and "10.1.2.3" not in html


def test_the_desktop_app_listens_on_the_network_when_asked(tmp_path, monkeypatch):
    monkeypatch.delenv("JOBALERTS_DB", raising=False)
    home = tmp_path / "home"
    assert not desktop.lan_enabled(home)
    store = Store(home / "data" / "jobalerts.db")
    v = vault.Vault(store, b"d" * 32)
    ui = UiSettings(store, v)
    ui.save({"web.lan": True})
    store.close()
    assert desktop.lan_enabled(home)


def test_digests_go_out_at_each_users_time(monkeypatch, tmp_path):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    fake, runner, alex = two_people(monkeypatch, tmp_path)
    person = runner.people[alex.id]
    tz = ZoneInfo(person.cfg.notify.timezone)
    morning = datetime.now(tz).replace(hour=7, minute=0) + timedelta(days=1)
    assert not person.digest_due(morning)              # the first time: the latest scheduled one counts as done
    assert not person.digest_due(morning.replace(minute=29))
    assert person.digest_due(morning.replace(minute=31) + timedelta(days=1))
