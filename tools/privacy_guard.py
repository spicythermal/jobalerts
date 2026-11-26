#!/usr/bin/env python3
"""Stop personal data and credentials from being committed or pushed to a public repository.

Installed as git hooks (.githooks/, enable with `git config core.hooksPath .githooks`):

    privacy_guard.py --staged          pre-commit: the staged changes
    privacy_guard.py --push            pre-push: every commit being pushed (git passes the refs on stdin)
    privacy_guard.py --tree            the whole current tree (CI)

It refuses:
- files that must never be public: data/, stories/, resumes, .env, keys, databases, documents;
- anything that looks like a credential (API tokens, private keys);
- any line containing one of your private terms (your name, email, employer, figures), listed in
  ~/.config/jobalerts/private-terms.txt or $JOBALERTS_PRIVATE_TERMS: one per line, `#` comments,
  `re:` for a regular expression. The list stays on your machine; it is never committed;
- on push, commits whose author or committer email isn't in $JOBALERTS_ALLOWED_EMAILS / the terms
  file's `allow-email:` lines (when any are set).

Only added lines are checked, so existing content doesn't block new work. Exit code 1 with the
reasons when something is refused. Never skip it with --no-verify; fix the content instead.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

FORBIDDEN_PATHS = [
    re.compile(p) for p in (
        r"^data/", r"(^|/)\.env$", r"^stories/", r"^resume_reference/", r"^relocation/", r"^resume_coverletter_tuning/",
        r"^config/profile\.md$", r"^config/companies\.yaml$", r"^config/candidates\.txt$", r"^config/config\.local\.yaml$",
        r"(^|/)secret\.key$", r"\.(db|sqlite3?|db-wal|db-shm)$", r"(^|/)id_(rsa|ed25519|ecdsa)[^/]*$",
        r"^(?!src/jobalerts/examples/).*\.(pdf|docx?|odt|odp|pptx?|tex|xlsx?)$",
    )
]
SECRETS = [
    (re.compile(p), what) for p, what in (
        (r"sk-ant-[A-Za-z0-9_-]{20,}", "an Anthropic API key"),
        (r"\bgh[pousr]_[A-Za-z0-9]{30,}", "a GitHub token"),
        (r"\bgithub_pat_[A-Za-z0-9_]{40,}", "a GitHub token"),
        (r"\bAKIA[0-9A-Z]{16}\b", "an AWS key"),
        (r"-----BEGIN [A-Z ]*PRIVATE KEY-----", "a private key"),
        (r"\bxox[baprs]-[A-Za-z0-9-]{20,}", "a Slack token"),
        (r"(?i)\b(api[_-]?key|app[_-]?key|token|password|secret)\s*[:=]\s*['\"][A-Za-z0-9_\-]{20,}['\"]",
         "a hard-coded credential"),
    )
]


def terms_file() -> Path:
    return Path(os.environ.get("JOBALERTS_PRIVATE_TERMS") or Path.home() / ".config" / "jobalerts" / "private-terms.txt")


def load_terms() -> tuple[list[tuple[re.Pattern, str]], set[str]]:
    patterns, emails = [], set(filter(None, os.environ.get("JOBALERTS_ALLOWED_EMAILS", "").split(",")))
    path = terms_file()
    if not path.is_file():
        return patterns, emails
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("allow-email:"):
            emails.add(line.split(":", 1)[1].strip())
        elif line.startswith("re:"):
            patterns.append((re.compile(line[3:].strip()), "a private term (pattern)"))
        else:
            patterns.append((re.compile(re.escape(line), re.IGNORECASE), "a private term"))
    return patterns, emails


def git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=True).stdout


def added_lines(diff: str):
    """(file, line) for each added line of a unified diff."""
    current = ""
    for line in diff.splitlines():
        if line.startswith("+++ "):
            current = line[6:] if line.startswith("+++ b/") else ""
        elif line.startswith("+") and not line.startswith("+++") and current:
            yield current, line[1:]


def check_diff(diff: str, names: list[str], where: str, terms) -> list[str]:
    problems = []
    for name in names:
        if any(p.search(name) for p in FORBIDDEN_PATHS):
            problems.append(f"{where}: {name} must never be in this repository")
    for name, line in added_lines(diff):
        for pattern, what in SECRETS + terms:
            if pattern.search(line):
                # never echo the match itself: it may be the secret
                problems.append(f"{where}: {name} adds a line with {what}")
                break
    return problems


def staged(terms) -> list[str]:
    names = git("diff", "--cached", "--name-only", "--diff-filter=ACMR").splitlines()
    return check_diff(git("diff", "--cached", "--unified=0", "--no-color"), names, "staged", terms)


def pushed(terms, emails: set[str]) -> list[str]:
    problems = []
    zero = "0" * 40
    for line in sys.stdin.read().splitlines():
        parts = line.split()
        if len(parts) < 4 or parts[1] == zero:      # deleting a ref
            continue
        local, remote = parts[1], parts[3]
        commits = git("rev-list", local, "--not", "--remotes") if remote == zero else git("rev-list", f"{remote}..{local}")
        for commit in commits.split():
            short = commit[:7]
            names = git("diff-tree", "--no-commit-id", "--name-only", "-r", "--diff-filter=ACMR", "--root", commit).splitlines()
            diff = git("show", "--format=", "--unified=0", "--no-color", commit)
            problems += check_diff(diff, names, f"commit {short}", terms)
            message = git("log", "-1", "--format=%B", commit)
            for pattern, what in SECRETS + terms:
                if pattern.search(message):
                    problems.append(f"commit {short}: its message has {what}")
                    break
            if emails:
                author, committer = git("log", "-1", "--format=%ae%n%ce", commit).split()
                for kind, email in (("author", author), ("committer", committer)):
                    if email not in emails:
                        problems.append(f"commit {short}: {kind} email isn't one of the allowed public addresses")
    return problems


def tree(terms) -> list[str]:
    problems = []
    for name in git("ls-files").splitlines():
        if any(p.search(name) for p in FORBIDDEN_PATHS):
            problems.append(f"tree: {name} must never be in this repository")
            continue
        try:
            text = Path(name).read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for number, line in enumerate(text.splitlines(), 1):
            hit = next((what for pattern, what in SECRETS + terms if pattern.search(line)), None)
            if hit:
                problems.append(f"tree: {name}:{number} has {hit}")
                break
    return problems


def main(argv: list[str]) -> int:
    terms, emails = load_terms()
    mode = argv[1] if len(argv) > 1 else "--staged"
    problems = {"--staged": lambda: staged(terms), "--push": lambda: pushed(terms, emails),
                "--tree": lambda: tree(terms)}[mode]()
    if problems:
        print("privacy guard: refused.", file=sys.stderr)
        for problem in problems[:50]:
            print(f"  {problem}", file=sys.stderr)
        print("Remove it (personal data belongs in the gitignored data/ folder), then try again. "
              f"Private terms: {terms_file()}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
