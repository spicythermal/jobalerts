"""Update jobalerts from its git repository: see what's new, and install it safely.

For an install that is a git checkout (a desktop, or a server without Docker). Docker installs are
updated from the host by tools/update-docker.sh: the container holds neither the repository nor a
way to rebuild itself.

An update only ever fast-forwards to the branch's upstream (e.g. origin/main). It refuses when you
changed shipped files or made commits of your own, so it never overwrites anything; everything of
yours is in data/, which git ignores. Installing: back up the database, move older installs' files
into data/ if the new version stops tracking them, fast-forward, reinstall the package, and run
`jobalerts check` with the new code. If that fails, it goes back to the previous version.
"""
from __future__ import annotations

import json
import logging
import subprocess
import sys
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
    status = UpdateStatus(checked_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
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
        raise RuntimeError(f"the new version's check failed: {(result.stdout + result.stderr).strip()[-800:]}")


def install(repo: Path | None = None, *, config: str = "config/config.yaml", store=None,
            reinstall_fn=reinstall, smoke_fn=smoke_test) -> tuple[bool, str]:
    """Fast-forward to the upstream and reinstall, or go back if the new version fails its check.
    Returns (installed, what happened)."""
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
