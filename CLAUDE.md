# Job Alerts – agent rules

The design is in `~/.claude/plans/hi-there-i-need-unified-robin.md`. Work is tracked in `PLAN.md`.

## Autonomous execution
- Work through `PLAN.md` from top to bottom. Pick the first `- [ ]` task, implement it, run its `verify:` step, fix any failures, then check it off as `- [x]` and add a line to the Progress log. Move straight on to the next task. Do not stop to summarize or ask whether to continue.
- Check a task off only after its verification actually passed. If verification can't run here (no Docker, no API key), mark the task `- [~]` with the reason and move on.
- If a task needs something only the user can provide (secrets, phone confirmation, real resume or profile content, a homelab deploy target), mark it `- [~] … (needs user: <what>)` and continue with the next task. Use sensible placeholders so later work isn't blocked.
- Stop only when no `- [ ]` tasks remain, or when every remaining task is truly blocked. In that case, add a line `BLOCKED: <reason>` to `PLAN.md`. The Stop hook (`.claude/hooks/plan-gate.sh`) enforces this.
- Commit after each phase with a descriptive message.
- More than one Claude session may work in this folder. Re-read `PLAN.md` before you pick up a task. Don't redo a task that another session has already checked off.

## Conventions
- Python 3.12, `src/` layout, httpx async, pydantic, PyYAML, sqlite3, pytest. Keep it plain and readable, like the user's other homelab scripts.
- Secrets go only in `.env` (gitignored). Never commit them. Keep `.env.example` up to date.
- Never scrape LinkedIn or Indeed. Respect the per-host rate limits in the design.
