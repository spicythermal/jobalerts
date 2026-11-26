"""Update jobalerts from its git repository: see what's new, and install it safely.

For an install that is a git checkout (a desktop, or a server without Docker). Docker installs are
updated from the host by tools/update-docker.sh: the container holds neither the repository nor a
way to rebuild itself. The desktop app (Windows installer, Mac app) updates from GitHub Releases
instead: check_release() and install_release() below.

An update only ever fast-forwards to the branch's upstream (e.g. origin/main). It refuses when you
changed shipped files or made commits of your own, so it never overwrites anything; everything of
yours is in data/, which git ignores. Installing: back up the database, move older installs' files
into data/ if the new version stops tracking them, fast-forward, reinstall the package, and run
`jobalerts check` with the new code. If that fails, it goes back to the previous version.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import re
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

# Files older versions tracked that are yours (see tools/migrate_user_data.sh).
LEGACY_PERSONAL = ["stories", "resume_reference", "relocation", "config/profile.md", "config/companies.yaml",
                   "config/candidates.txt"]
STATUS_META = "update.status"


@dataclass
class UpdateStatus:
    repo: str = ""
    branch: str = ""
    upstream: str = ""
    current: str = ""              # short commit hash
    current_date: str = ""
    behind: int = 0                # commits on the upstream not here yet
    ahead: int = 0                 # commits here not on the upstream
    commits: list[str] = field(default_factory=list)   # "abc1234 subject", newest first
    latest: str = ""               # the upstream's short hash
    problem: str = ""              # why it can't update itself ("" = it can)
    checked_at: str = ""
    notes: list[str] = field(default_factory=list)     # desktop app: the release's notes
    download: str = ""             # desktop app: this computer's installer
    checksums: str = ""            # desktop app: the release's SHA256SUMS.txt
    signature: str = ""            # desktop app: SHA256SUMS.txt.sig, signed with the release key

    @property
    def available(self) -> bool:
        return self.behind > 0

    @property
    def can_install(self) -> bool:
        return self.available and not self.problem

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, text: str | None) -> UpdateStatus | None:
        try:
            return cls(**json.loads(text)) if text else None
        except (TypeError, ValueError):
            return None


def find_repo(start: Path | None = None) -> Path | None:
    """The git checkout jobalerts runs from (an editable install), or None (installed from a package,
    or a container)."""
    here = (start or Path(__file__)).resolve()
    for folder in [here, *here.parents]:
        if (folder / ".git").exists() and (folder / "pyproject.toml").is_file():
            return folder
    return None


def git(repo: Path, *args: str, timeout: int = 120) -> str:
    result = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"git {args[0]}: {(result.stderr or result.stdout).strip()[:300]}")
    return result.stdout.strip()


def check(repo: Path | None = None, fetch: bool = True) -> UpdateStatus:
    """What's new on the upstream, and whether it can be installed automatically."""
    if repo is None and is_packaged():
        return check_release()
    status = UpdateStatus(checked_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                          current=os.environ.get("JOBALERTS_VERSION", ""))   # set in the Docker image
    repo = repo or find_repo()
    if repo is None:
        status.problem = ("not a git checkout: a Docker install is updated from the host with "
                          "tools/update-docker.sh; a package install with pip")
        return status
    status.repo = str(repo)
    try:
        status.branch = git(repo, "rev-parse", "--abbrev-ref", "HEAD")
        status.current = git(repo, "rev-parse", "--short", "HEAD")
        status.current_date = git(repo, "log", "-1", "--format=%cs")
        try:
            status.upstream = git(repo, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
        except RuntimeError:
            status.problem = f"the branch {status.branch!r} has no upstream to update from"
            return status
        if fetch:
            git(repo, "fetch", "--quiet", status.upstream.split("/", 1)[0])
        status.latest = git(repo, "rev-parse", "--short", "@{u}")
        ahead, behind = git(repo, "rev-list", "--left-right", "--count", "HEAD...@{u}").split()
        status.ahead, status.behind = int(ahead), int(behind)
        if status.behind:
            status.commits = git(repo, "log", "--format=%h %s", "-n", "30", "HEAD..@{u}").splitlines()
        changed = git(repo, "status", "--porcelain", "--untracked-files=no").splitlines()
        if changed:
            names = ", ".join(line.split(None, 1)[-1] for line in changed[:5]) + (" …" if len(changed) > 5 else "")
            status.problem = (f"shipped files were changed here ({names}); put your changes in data/ or "
                              "commit them, then update by hand")
        elif status.ahead:
            status.problem = f"{status.ahead} commit(s) here aren't on {status.upstream}; update by hand"
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as e:
        status.problem = f"couldn't check for updates: {e}"
    return status


def reinstall(repo: Path) -> None:
    """Install the updated package into the Python that runs jobalerts (editable if it was)."""
    import jobalerts
    editable = Path(jobalerts.__file__).resolve().is_relative_to(repo.resolve())
    args = [sys.executable, "-m", "pip", "install", "--quiet", "--disable-pip-version-check"]
    args += ["-e", str(repo)] if editable else [str(repo)]
    result = subprocess.run(args, capture_output=True, text=True, timeout=900)
    if result.returncode:
        raise RuntimeError(f"pip install failed: {(result.stderr or result.stdout).strip()[-500:]}")


def smoke_test(repo: Path, config: str) -> None:
    """`jobalerts check` with the new code, in a new process: config loads, database opens and
    upgrades, the web UI builds."""
    result = subprocess.run([sys.executable, "-m", "jobalerts", "-c", config, "check"], cwd=repo,
                            capture_output=True, text=True, timeout=300)
    if result.returncode:
        lines = [line.strip() for line in (result.stdout + result.stderr).splitlines() if line.strip()]
        raise RuntimeError("the new version's check failed: " + " / ".join(lines[-2:])[-400:])


def install(repo: Path | None = None, *, config: str = "config/config.yaml", store=None,
            reinstall_fn=reinstall, smoke_fn=smoke_test) -> tuple[bool, str]:
    """Fast-forward to the upstream and reinstall, or go back if the new version fails its check.
    Returns (installed, what happened)."""
    if repo is None and is_packaged():
        return install_release(store)
    status = check(repo)
    if not status.can_install:
        return False, status.problem or "already up to date"
    repo = Path(status.repo)
    old = git(repo, "rev-parse", "HEAD")
    if git(repo, "ls-files", *LEGACY_PERSONAL):
        # An older install whose files the new version stops tracking: move them into data/ first.
        script = git(repo, "show", "@{u}:tools/migrate_user_data.sh")
        result = subprocess.run(["sh", "-s"], input=script, cwd=repo, capture_output=True, text=True, timeout=300)
        if result.returncode:
            return False, f"moving your files into data/ failed; nothing was updated: {result.stderr.strip()[-300:]}"
        log.info("Moved your files into data/ before updating:\n%s", result.stdout.strip())
    if store is not None:
        store.backup("before-update")
    git(repo, "merge", "--ff-only", "--quiet", "@{u}")
    new = git(repo, "rev-parse", "--short", "HEAD")
    try:
        reinstall_fn(repo)
        smoke_fn(repo, config)
    except Exception as e:
        log.error("Update to %s failed, going back to %s: %s", new, old[:7], e)
        git(repo, "reset", "--hard", "--quiet", old)     # only shipped files; data/ is untouched (ignored)
        try:
            reinstall_fn(repo)
        except Exception as again:
            log.error("Reinstalling the previous version failed too: %s", again)
        return False, f"the update to {new} failed and was rolled back to {old[:7]}: {e}"
    log.info("Updated %s → %s (%d commits)", old[:7], new, status.behind)
    return True, f"updated {old[:7]} → {new} ({status.behind} change{'s' if status.behind != 1 else ''})"


# ---------------------------------------------------------------- the desktop app: GitHub Releases
RELEASES = os.environ.get("JOBALERTS_RELEASES", "spicythermal/jobalerts")   # owner/repository
# The public half of the release signing key (tools/sign_release.py, docs/releasing.md); fingerprint
# 1a97649e9e94192a. The app installs a release only if its SHA256SUMS.txt is signed with this key.
RELEASE_KEY = "36ZgrVjrPmRS1EVrobtumk8uHIoYQkqKQ2Cg+SZQVeY="


def is_packaged() -> bool:
    """Running as the desktop app (a PyInstaller bundle), not from a checkout or the Docker image."""
    return bool(getattr(sys, "frozen", False))


def asset_suffix(system: str | None = None, machine: str | None = None) -> str:
    """The end of the release file name of the installer for this computer."""
    system = system or sys.platform
    if system == "win32":
        return "-windows-setup.exe"
    if system == "darwin":
        return "-mac-arm64.dmg" if (machine or platform.machine()) == "arm64" else "-mac-intel.dmg"
    return ""


def version_key(version: str) -> tuple[int, ...]:
    """'v1.2.3' -> (1, 2, 3); a suffix such as '-rc1' is ignored."""
    return tuple(int(n) for n in re.findall(r"\d+", version.lstrip("vV").split("-")[0])[:3])


def read_release(release: dict, current: str, suffix: str) -> UpdateStatus:
    status = UpdateStatus(checked_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                          current=current, upstream="GitHub releases")
    tag = release.get("tag_name", "")
    status.latest = tag.lstrip("vV")
    status.behind = 1 if version_key(tag) > version_key(current) else 0
    status.notes = [line.strip().lstrip("-* ").strip() for line in (release.get("body") or "").splitlines()
                    if line.strip() and not line.startswith("#")][:15]
    assets = {a.get("name", ""): a.get("browser_download_url", "") for a in release.get("assets") or []}
    status.download = next((url for name, url in assets.items() if suffix and name.endswith(suffix)), "")
    status.checksums = assets.get("SHA256SUMS.txt", "")
    status.signature = assets.get("SHA256SUMS.txt.sig", "")
    if status.behind and not status.download:
        status.problem = "this release has no installer for this computer; download it from the releases page"
    elif status.behind and not (status.checksums and status.signature):
        status.problem = "this release isn't signed; not installing it"
    return status


def check_release(get=None) -> UpdateStatus:
    """The newest release (not pre-releases) against this version."""
    import httpx
    from . import __version__
    try:
        r = (get or httpx.get)(f"https://api.github.com/repos/{RELEASES}/releases/latest", timeout=30,
                               headers={"Accept": "application/vnd.github+json",
                                        "User-Agent": f"jobalerts/{__version__}"}, follow_redirects=True)
        r.raise_for_status()
        return read_release(r.json(), __version__, asset_suffix())
    except Exception as e:
        status = UpdateStatus(checked_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                              current=__version__, upstream="GitHub releases")
        status.problem = f"couldn't check for updates: {type(e).__name__}: {e}"[:300]
        return status


def _download(url: str, target: Path) -> None:
    import httpx
    with httpx.stream("GET", url, follow_redirects=True, timeout=120) as r:
        r.raise_for_status()
        with open(target, "wb") as f:
            for chunk in r.iter_bytes():
                f.write(chunk)


def signed(data: bytes, signature: str, key: str | None = None) -> bool:
    """True if `signature` (base64 Ed25519) signs `data` with the release key."""
    import base64
    import binascii

    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    try:
        public = Ed25519PublicKey.from_public_bytes(base64.b64decode(key or RELEASE_KEY))
        public.verify(base64.b64decode(signature.strip(), validate=True), data)
        return True
    except (InvalidSignature, ValueError, binascii.Error):
        return False


def verify(path: Path, sums: str) -> bool:
    """True if SHA256SUMS.txt lists this file with its hash."""
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    for line in sums.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*") == path.name:
            return parts[0].lower() == digest
    return False


MAC_SWAP = """#!/bin/sh
# Replace the running jobalerts.app with the downloaded one, then start it (written by jobalerts' updater).
while kill -0 "$1" 2>/dev/null; do sleep 1; done
app="$2"; dmg="$3"; mnt=$(mktemp -d)
hdiutil attach -nobrowse -readonly -mountpoint "$mnt" "$dmg" >/dev/null || { open "$app"; exit 1; }
if [ -d "$mnt/jobalerts.app" ]; then
  rm -rf "$app.old" && mv "$app" "$app.old" && ditto "$mnt/jobalerts.app" "$app" && rm -rf "$app.old" \
    || { rm -rf "$app"; mv "$app.old" "$app"; }
fi
hdiutil detach "$mnt" >/dev/null
open "$app"
"""


def install_release(store=None, status: UpdateStatus | None = None) -> tuple[bool, str]:
    """Download this computer's installer from the newest release, check SHA256SUMS.txt's signature
    and the installer's hash in it, and start it; the app then exits (cli.restart_into_new_version) and the installer starts the new one.
    Windows: the setup program, silently. Mac: a script swaps jobalerts.app once this process ends."""
    status = status or check_release()
    if not status.can_install:
        return False, status.problem or "already up to date"
    folder = Path(tempfile.mkdtemp(prefix="jobalerts-update-"))
    target = folder / status.download.rsplit("/", 1)[-1]
    try:
        _download(status.download, target)
        sums, sig = folder / "SHA256SUMS.txt", folder / "SHA256SUMS.txt.sig"
        if not status.checksums or not status.signature:
            return False, "the release has no signed SHA256SUMS.txt; not installing an unchecked download"
        _download(status.checksums, sums)
        _download(status.signature, sig)
        if not signed(sums.read_bytes(), sig.read_text()):
            return False, "the release's signature doesn't match this app's release key; not installed"
        if not verify(target, sums.read_text()):
            return False, "the download doesn't match the release's SHA256SUMS.txt; not installed"
    except Exception as e:
        return False, f"download failed: {type(e).__name__}: {e}"[:300]
    if store is not None:
        store.backup("before-update")
    if sys.platform == "win32":
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        subprocess.Popen([str(target), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS",
                          "/FORCECLOSEAPPLICATIONS"], creationflags=flags, close_fds=True)
    elif sys.platform == "darwin":
        app = Path(sys.executable).resolve().parents[2]      # …/jobalerts.app/Contents/MacOS/jobalerts
        if app.suffix != ".app":
            return False, f"not running from jobalerts.app ({app}); install the new version by hand"
        script = folder / "swap.sh"
        script.write_text(MAC_SWAP)
        subprocess.Popen(["/bin/sh", str(script), str(os.getpid()), str(app), str(target)], start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        return False, "updates for this system aren't supported; download the new version by hand"
    log.info("Installing version %s from %s", status.latest, target.name)
    return True, f"installing version {status.latest}; jobalerts restarts by itself"
