"""The desktop app: what the Windows installer starts.

It runs the same service as `jobalerts run`, set up for one person on their own computer:

- data in the user's app folder (Windows: %LOCALAPPDATA%\\jobalerts, elsewhere
  ~/.local/share/jobalerts; JOBALERTS_HOME overrides), with the shipped
  config/ copied next to it on every start;
- the web UI on this computer only (127.0.0.1:8765);
- the browser opens on start, at the setup page with the setup code filled in the first time;
- a tray icon: Open, Start at login, Quit;
- starting it again while it runs only opens the browser;
- start at login (on by default the first time; the icon's menu turns it off).

`--background` (used at login) doesn't open the browser. `--self-test` starts it in a temporary
folder, checks that the web UI answers, and exits 0 or 1 (the release build runs it).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import logging.handlers
import os
import secrets
import shutil
import sys
import tempfile
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path
from types import SimpleNamespace

log = logging.getLogger("jobalerts.desktop")
APP_ID = "io.github.spicythermal.jobalerts"
PORT = 8765


def app_home() -> Path:
    if os.environ.get("JOBALERTS_HOME"):
        return Path(os.environ["JOBALERTS_HOME"])
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "jobalerts"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "jobalerts"


def bundle_root() -> Path:
    """Where the shipped config/ is: inside the app bundle, or the repository when run from a checkout."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parents[2]


def prepare(home: Path) -> Path:
    """Copy the shipped config/ into the app folder (replacing the previous version's) and make data/.
    Returns the config file. The relative paths in config.yaml then find data/ next to it."""
    home.mkdir(parents=True, exist_ok=True)
    (home / "data").mkdir(exist_ok=True)
    source, target = bundle_root() / "config", home / "config"
    fresh = home / "config.new"
    shutil.rmtree(fresh, ignore_errors=True)
    shutil.copytree(source, fresh)
    shutil.rmtree(target, ignore_errors=True)
    fresh.rename(target)
    return target / "config.yaml"


def setup_logging(home: Path) -> Path:
    path = home / "data" / "jobalerts.log"
    handler = logging.handlers.RotatingFileHandler(path, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("paho").setLevel(logging.CRITICAL)
    from .vault import install_log_redaction
    install_log_redaction()
    return path


def answering(port: int, timeout: float = 1.0) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=timeout) as r:
            return r.status == 200
    except OSError:
        return False


def needs_setup(port: int) -> bool:
    """No password yet: /setup shows the form (with a password it redirects to /login)."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/setup", timeout=5) as r:
            return not r.geturl().endswith("/login")
    except OSError:
        return False


def wait_until_up(port: int, seconds: float) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if answering(port):
            return True
        time.sleep(0.5)
    return False


class Service:
    """`jobalerts run` in a background thread with its own event loop, stoppable from the tray."""

    def __init__(self, config: Path):
        self.config = config
        self.loop = asyncio.new_event_loop()
        self.task: asyncio.Task | None = None
        self.thread = threading.Thread(target=self._run, name="jobalerts", daemon=True)
        self.exit_code = 0

    def _run(self) -> None:
        from .cli import cmd_run
        from .config import load_config
        from .userdata import seed_user_files
        asyncio.set_event_loop(self.loop)
        try:
            cfg = load_config(self.config)
            seed_user_files(cfg)
            self.task = self.loop.create_task(cmd_run(cfg, SimpleNamespace(config=str(self.config))))
            self.exit_code = self.loop.run_until_complete(self.task) or 0
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("jobalerts stopped with an error")
            self.exit_code = 1
        finally:
            self.loop.close()

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        if self.task and not self.loop.is_closed():
            self.loop.call_soon_threadsafe(self.task.cancel)
        self.thread.join(timeout=20)


# ---------------------------------------------------------------- start at login

def autostart_enabled() -> bool:
    if sys.platform == "win32":
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as key:
                winreg.QueryValueEx(key, "jobalerts")
                return True
        except OSError:
            return False
    return False


def set_autostart(on: bool) -> bool:
    """Start at login on or off. False if this system or this copy (not installed) can't."""
    if not getattr(sys, "frozen", False):
        return False
    if sys.platform == "win32":
        import winreg
        path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_SET_VALUE) as key:
            if on:
                winreg.SetValueEx(key, "jobalerts", 0, winreg.REG_SZ, f'"{sys.executable}" --background')
            else:
                try:
                    winreg.DeleteValue(key, "jobalerts")
                except OSError:
                    pass
        return True
    return False


def first_run_autostart(home: Path) -> None:
    """Turn start at login on once, the first time; after that the user's choice in the menu stands."""
    marker = home / "data" / "desktop.json"
    try:
        state = json.loads(marker.read_text()) if marker.is_file() else {}
    except ValueError:
        state = {}
    if not state.get("autostart_set"):
        if set_autostart(True):
            log.info("Start at login turned on")
        state["autostart_set"] = True
        marker.write_text(json.dumps(state))


# ---------------------------------------------------------------- the tray icon

def _icon_image():
    from PIL import Image, ImageDraw
    size = 64
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((4, 4, size - 4, size - 4), radius=14, fill=(36, 86, 166, 255))
    draw.rectangle((36, 14, 44, 42), fill="white")                    # a "J"
    draw.arc((16, 26, 44, 52), start=0, end=180, fill="white", width=8)
    return image


def run_tray(service: Service, open_ui) -> bool:
    """Show the icon until Quit (or the service stops). False if no tray is available here."""
    try:
        import pystray
    except Exception as e:
        log.info("No tray icon (%s); running without one", e)
        return False

    def quit_app(icon, _item):
        icon.visible = False
        service.stop()
        icon.stop()

    def toggle_autostart(_icon, _item):
        set_autostart(not autostart_enabled())

    menu = pystray.Menu(
        pystray.MenuItem("Open jobalerts", lambda *_: open_ui(), default=True),
        pystray.MenuItem("Start at login", toggle_autostart, checked=lambda _item: autostart_enabled(),
                         visible=getattr(sys, "frozen", False)),
        pystray.MenuItem("Quit", quit_app),
    )
    icon = pystray.Icon("jobalerts", _icon_image(), "jobalerts", menu)

    def watch(icon):
        icon.visible = True
        service.thread.join()        # the service stopped by itself (an update, an error): close the icon
        icon.stop()
    try:
        icon.run(setup=watch)
    except Exception as e:
        log.info("Tray icon failed (%s); running without one", e)
        return False
    return True


# ---------------------------------------------------------------- main

def self_test() -> int:
    """Start in a temporary folder on a spare port, check the web UI answers, stop."""
    import socket
    home = Path(tempfile.mkdtemp(prefix="jobalerts-selftest-"))
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    os.environ.update(JOBALERTS_HOME=str(home), JOBALERTS_WEB_PORT=str(port))
    config = prepare(home)
    log_path = setup_logging(home)
    os.chdir(home)
    service = Service(config)
    service.start()
    ok = wait_until_up(port, 90)
    if ok:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/setup", timeout=10) as r:
                ok = r.status == 200 and b"Set up job alerts" in r.read()
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/static/app.css", timeout=10) as r:
                ok = ok and r.status == 200
        except OSError:
            ok = False
    service.stop()
    report = f"self-test: {'ok' if ok else 'FAILED'}\n"
    if not ok:
        report += log_path.read_text(encoding="utf-8", errors="replace")[-4000:]
    print(report, file=sys.stderr)
    if os.environ.get("JOBALERTS_SELFTEST_REPORT"):     # a windowed app has no console: CI reads this file
        Path(os.environ["JOBALERTS_SELFTEST_REPORT"]).write_text(report, encoding="utf-8")
    return 0 if ok else 1


def lan_enabled(home: Path) -> bool:
    """Settings → Connections, "other computers on this network can open it" (web.lan, the install's)."""
    path = Path(os.environ.get("JOBALERTS_DB") or home / "data" / "jobalerts.db")
    if not path.is_file():
        return False
    import sqlite3
    try:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            row = db.execute("SELECT value FROM settings_by_user WHERE user_id=0 AND key='web.lan'").fetchone()
        finally:
            db.close()
        return bool(row) and json.loads(row[0]) is True
    except (sqlite3.Error, ValueError):
        return False


def main(argv: list[str] | None = None) -> int:
    for name in ("stdout", "stderr"):     # a windowed app has no console
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w"))
    parser = argparse.ArgumentParser(prog="jobalerts-desktop")
    parser.add_argument("--background", action="store_true", help="don't open the browser (start at login)")
    parser.add_argument("--self-test", action="store_true", help="start in a temporary folder, check, exit")
    args, _ = parser.parse_known_args(argv)
    if args.self_test:
        return self_test()

    home = app_home()
    port = int(os.environ.get("JOBALERTS_WEB_PORT", PORT))
    base = f"http://127.0.0.1:{port}"
    if answering(port):                 # already running: just show it
        webbrowser.open(base + "/")
        return 0
    config = prepare(home)
    setup_logging(home)
    os.chdir(home)
    code = f"{secrets.token_hex(2).upper()}-{secrets.token_hex(2).upper()}"
    bind = "0.0.0.0" if lan_enabled(home) else "127.0.0.1"   # this computer only, unless set otherwise
    os.environ.update(JOBALERTS_WEB_BIND=bind, JOBALERTS_WEB_PORT=str(port), JOBALERTS_SETUP_CODE=code)

    from .runstate import InstanceLock
    probe = InstanceLock(home / "data" / "jobalerts.lock")
    if not probe.acquire():             # running but not answering yet (starting up)
        if wait_until_up(port, 30):
            webbrowser.open(base + "/")
        return 0
    probe.release()

    first_run_autostart(home)
    service = Service(config)
    service.start()

    def open_ui() -> None:
        webbrowser.open(f"{base}/setup?code={code}" if needs_setup(port) else base + "/")

    if wait_until_up(port, 60):
        if not args.background:
            open_ui()
    else:
        log.error("The web UI didn't start; see this log")
    if not run_tray(service, open_ui):
        service.thread.join()
    return service.exit_code


if __name__ == "__main__":
    sys.exit(main())
