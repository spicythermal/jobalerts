# Job Alerts – agent rules

The architecture is in `docs/architecture.md`. Work is tracked in `PLAN.md`.

## Autonomous execution
- Work through `PLAN.md` from top to bottom. Pick the first `- [ ]` task, implement it, run its `verify:` step, fix any failures, then check it off as `- [x]` and add a line to the Progress log. Move straight on to the next task. Do not stop to summarize or ask whether to continue.
- Check a task off only after its verification actually passed. If verification can't run here (no Docker, no API key), mark the task `- [~]` with the reason and move on.
- If a task needs something only the user can provide (secrets, phone confirmation, real resume or profile content, a homelab deploy target), mark it `- [~] … (needs user: <what>)` and continue with the next task. Use sensible placeholders so later work isn't blocked.
- Stop only when no `- [ ]` tasks remain, or when every remaining task is truly blocked. In that case, add a line `BLOCKED: <reason>` to `PLAN.md`. The Stop hook (`.claude/hooks/plan-gate.sh`) enforces this.
- Commit after each phase with a descriptive message, and push to origin (the public GitHub repository).
- This repository is public. The privacy guard (`tools/privacy_guard.py`, hooks in `.githooks/`) checks every
  commit and push; never bypass it (`--no-verify`), never add another remote, and never copy files in from the
  maintainer's private repository or data folder. If the guard refuses, remove the personal data instead.
- More than one Claude session may work in this folder. Re-read `PLAN.md` before you pick up a task. Don't redo a task that another session has already checked off.

## Conventions
- Python 3.12, `src/` layout, httpx async, pydantic, PyYAML, sqlite3, pytest; the web UI is FastAPI + Jinja + htmx (no Node build). Keep it plain and readable.
- Secrets go only in `.env` (gitignored) or the encrypted vault behind the Settings page. Never commit them, never send them to the browser. Keep `.env.example` up to date.
- This is an open-source project (GPL-3.0). Personal data (profile, stories, resumes, watch list, salary, home, household) lives only in the gitignored data folder; never write it into tracked files, tests or docs. Use neutral examples.
- Never scrape LinkedIn or Indeed. Respect the per-host rate limits in the design.
