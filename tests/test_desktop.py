"""The desktop app: its folder, updates from GitHub Releases, and the launcher's hooks into the web UI."""
import hashlib
import re
from types import SimpleNamespace

from fastapi.testclient import TestClient

from jobalerts import desktop, updater, vault
from jobalerts.config import Config, load_config
from jobalerts.http import HttpClient
from jobalerts.notify import Notifier
from jobalerts.scheduler import Runner
from jobalerts.store import Store
from jobalerts.web.app import create_app

RELEASE = {
    "tag_name": "v1.4.0",
    "body": "## What's changed\n- Filter page\n- Faster polls\n\n**Full Changelog**: https://example.invalid/compare",
    "assets": [
        {"name": "jobalerts-1.4.0-windows-setup.exe", "browser_download_url": "https://dl.invalid/v1.4.0/jobalerts-1.4.0-windows-setup.exe"},
        {"name": "jobalerts-1.4.0-mac-arm64.dmg", "browser_download_url": "https://dl.invalid/v1.4.0/jobalerts-1.4.0-mac-arm64.dmg"},
        {"name": "SHA256SUMS.txt", "browser_download_url": "https://dl.invalid/v1.4.0/SHA256SUMS.txt"},
        {"name": "SHA256SUMS.txt.sig", "browser_download_url": "https://dl.invalid/v1.4.0/SHA256SUMS.txt.sig"},
    ],
}


def release_keypair():
    """A throwaway release key: (private key, base64 public key)."""
    import base64

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    key = Ed25519PrivateKey.generate()
    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return key, base64.b64encode(raw).decode()


def test_versions_and_assets():
    assert updater.version_key("v1.10.0") > updater.version_key("1.9.3")
    assert updater.version_key("v2.0.0-rc1") == (2, 0, 0)
    assert updater.asset_suffix("win32") == "-windows-setup.exe"
    assert updater.asset_suffix("darwin", "arm64") == "-mac-arm64.dmg"
    assert updater.asset_suffix("darwin", "x86_64") == "-mac-intel.dmg"
    assert updater.asset_suffix("linux") == ""


def test_reading_a_release():
    s = updater.read_release(RELEASE, "1.3.2", "-windows-setup.exe")
    assert (s.latest, s.behind, s.available, s.can_install) == ("1.4.0", 1, True, True)
    assert s.download == "https://dl.invalid/v1.4.0/jobalerts-1.4.0-windows-setup.exe" and s.checksums == "https://dl.invalid/v1.4.0/SHA256SUMS.txt"
    assert s.notes[:2] == ["Filter page", "Faster polls"]
    s = updater.read_release(RELEASE, "1.4.0", "-windows-setup.exe")
    assert not s.available
    s = updater.read_release(RELEASE, "1.3.2", "-mac-intel.dmg")      # no Intel build in this release
    assert s.available and not s.can_install and "no installer" in s.problem
    s = updater.read_release({**RELEASE, "assets": RELEASE["assets"][:3]}, "1.3.2", "-windows-setup.exe")
    assert s.available and not s.can_install and "isn't signed" in s.problem
    s = updater.check_release(get=lambda *a, **k: (_ for _ in ()).throw(OSError("offline")))
    assert "couldn't check" in s.problem and not s.available


def test_install_release_checks_the_download(tmp_path, monkeypatch):
    import base64
    payload = b"installer bytes"
    started = []
    release_key, public = release_keypair()
    other_key, _ = release_keypair()
    monkeypatch.setattr(updater, "RELEASE_KEY", public)

    def sums_text():
        return f"{hashlib.sha256(sums_for).hexdigest()}  jobalerts-1.4.0-windows-setup.exe\n"

    def fake_download(url, target):
        if url.endswith("SHA256SUMS.txt"):
            target.write_text(sums_text())
        elif url.endswith(".sig"):
            target.write_text(base64.b64encode(signer.sign(sums_text().encode())).decode() + "\n")
        else:
            target.write_bytes(payload)
    monkeypatch.setattr(updater, "_download", fake_download)
    monkeypatch.setattr(updater.sys, "platform", "win32")
    monkeypatch.setattr(updater.subprocess, "Popen", lambda args, **k: started.append(args))
    monkeypatch.setattr(updater.tempfile, "mkdtemp", lambda prefix: str(tmp_path))
    status = updater.read_release(RELEASE, "1.3.2", "-windows-setup.exe")

    sums_for, signer = payload, other_key                   # signed, but not with the release key
    ok, message = updater.install_release(status=status)
    assert not ok and "signature doesn't match" in message and started == []
    assert not updater.signed(b"x", "not base64!") and not updater.signed(b"x", "")

    sums_for, signer = b"something else", release_key      # signed, but the installer isn't the one listed
    ok, message = updater.install_release(status=status)
    assert not ok and "doesn't match the release's SHA256SUMS" in message and started == []

    sums_for = payload
    backups = []
    ok, message = updater.install_release(store=SimpleNamespace(backup=backups.append), status=status)
    assert ok and "1.4.0" in message and backups == ["before-update"], message
    assert started[0][0].endswith("jobalerts-1.4.0-windows-setup.exe") and "/VERYSILENT" in started[0]

    no_sums = updater.read_release({**RELEASE, "assets": RELEASE["assets"][:2]}, "1.3.2", "-windows-setup.exe")
    ok, message = updater.install_release(status=no_sums)
    assert not ok and "isn't signed" in message
    unsigned = updater.UpdateStatus(**{**status.__dict__, "signature": ""})
    ok, message = updater.install_release(status=unsigned)
    assert not ok and "signed" in message and len(started) == 1


def test_the_release_key_is_the_maintainers():
    import base64
    import hashlib as h
    assert h.sha256(base64.b64decode(updater.RELEASE_KEY)).hexdigest()[:16] == "1a97649e9e94192a"


def test_packaged_app_checks_releases_not_git(monkeypatch):
    monkeypatch.setattr(updater.sys, "frozen", True, raising=False)
    monkeypatch.setattr(updater, "check_release", lambda: updater.UpdateStatus(current="1.0.0", latest="1.1.0", behind=1))
    assert updater.check().latest == "1.1.0"


def test_app_folder_gets_the_shipped_config(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("JOBALERTS_HOME", str(home))
    assert desktop.app_home() == home
    config = desktop.prepare(home)
    assert config == home / "config" / "config.yaml" and (home / "data").is_dir()
    assert (home / "config" / "tax_data").is_dir() and (home / "config" / "col_data.json").is_file()
    (home / "config" / "stale.txt").write_text("from an older version")
    desktop.prepare(home)                                          # replaced on every start
    assert not (home / "config" / "stale.txt").exists()
    monkeypatch.delenv("JOBALERTS_LOCAL_CONFIG", raising=False)
    monkeypatch.delenv("JOBALERTS_DB", raising=False)
    monkeypatch.chdir(home)
    monkeypatch.setenv("JOBALERTS_WEB_BIND", "127.0.0.1")
    monkeypatch.setenv("JOBALERTS_WEB_PORT", "18999")
    monkeypatch.setenv("JOBALERTS_SETUP_CODE", "WXYZ-0001")
    cfg = load_config(config)
    assert (cfg.web.bind, cfg.web.port, cfg.web.setup_code) == ("127.0.0.1", 18999, "WXYZ-0001")
    assert cfg.resolve(cfg.scoring.profile_path) == home / "data" / "profile.md"
    assert cfg.resolve(cfg.companies_file) == home / "data" / "companies.yaml"
    assert not desktop.set_autostart(True)                          # only the installed app does this


def test_setup_code_from_the_launcher_is_filled_in(tmp_path, monkeypatch):
    for name in vault.SECRETS:
        monkeypatch.delenv(name, raising=False)
    cfg = Config(config_dir=tmp_path, database=str(tmp_path / "jobs.db"), pay={"enabled": False},
                 web={"setup_code": "abcd-1234"})
    runner = Runner(cfg, Store(cfg.database), HttpClient(), Notifier(cfg.notify, dry_run=True))
    app = create_app(runner, b"d" * 32)
    assert app.state.web.setup_code == "ABCD-1234"
    with TestClient(app, follow_redirects=False) as client:
        html = client.get("/setup?code=ABCD-1234").text
        assert 'value="ABCD-1234"' in html and "Enter the setup code" not in html
        token = re.search(r'name="csrf" value="([^"]+)"', html).group(1)
        r = client.post("/setup", data={"csrf": token, "code": "ABCD-1234", "password": "a long password here",
                                        "again": "a long password here"})
        assert r.status_code == 303 and r.headers["location"] == "/"
