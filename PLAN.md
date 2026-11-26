# Job Alerts – Execution Plan

Architecture: `docs/architecture.md`. Earlier phases (0–16: polling, ATS adapters, AI scoring, pay index,
taxes, map) are done.

Legend: `- [ ]` todo · `- [x]` done (verified) · `- [~]` deferred — needs the user (say why on the same line).
The Stop hook keeps Claude working while any `- [ ]` remains. Add `BLOCKED: <reason>` on its own line to let it stop.

## Goals for the open-source release (2026-11-24)
- A native web UI next to Home Assistant: **Jobs** (recommendations; the home page, where alerts land),
  **Boards** (which job boards are polled and how they're doing), the **cost-of-living map**, a **Stories**
  editor (what the AI grades jobs against) and **Settings** (current salary, home, household, stay-or-move
  preference, thresholds, notifications, API keys).
- Personal numbers and API keys stored protected on the backend; keys are write-only from the UI.
- Password login on the LAN; a read-only share link for the Home Assistant map card.
- One program for both a 24/7 server and a PC that's only on sometimes: after a gap it diffs the boards,
  sends one catch-up push and a report, and sends a missed digest. No mode switch.
- Personal files out of git: everything of yours lives in the data folder; examples ship instead. GPL-3.0.

## Phase 17 – Prepare the repo for sharing
- [x] Data folder for your own files (profile, stories, resumes, watch list, `config.local.yaml`); shipped examples in `src/jobalerts/examples/`, seeded on first run; neutral `config/config.yaml` defaults → verify: tests (layering, value sources, example fallback, seeding never overwrites)
- [x] `config.local.yaml` merged on top of the defaults; an old `config.yaml` left as `data/config.migrate.yaml` turns into one on the next start (only changed values, no file paths, `${VAR}` never expanded) → verify: tests + a real install migrated (its settings carried over; boards, stories and resumes load)
- [x] Personal files untracked and gitignored (stories/, resume_reference/, relocation/, profile, watch list, candidates); `tools/migrate_user_data.sh` for an existing server, run before `git pull` → verify: `git ls-files` has none of them; script rehearsed on a scratch clone
- [x] GPL-3.0 `LICENSE` + pyproject license; README/checklist examples without personal figures; fresh PLAN.md; `docs/architecture.md`; CLAUDE.md points to it → verify: personal-string scan of tracked files
- [x] Commit + push Phase 17

## Phase 18 – Web app foundation (`src/jobalerts/web/`)
- [x] FastAPI + Jinja + htmx (vendored) app; uvicorn task inside `jobalerts run`; `jobalerts web` for the UI alone; `/map` moves in; `mapserver.py` retired; base layout with nav, light/dark, phone width → verify: TestClient tests; map page = `colmap.render` output
- [x] Auth: first-run setup code in the log, scrypt password, rate-limited login, signed HttpOnly session cookie (30 d), CSRF on every POST, `jobalerts set-password` → verify: tests (redirects, setup code required, backoff, CSRF rejected)
- [x] Read-only share token for the Home Assistant map card (`/map?token=…`, rotatable; frame-ancestors for the HA origin only); security headers (CSP, nosniff, Referrer-Policy, no-store) → verify: tests (token opens /map only)
- [x] Commit + push Phase 18

## Phase 19 – Settings, intent and the secret vault
- [x] `vault.py`: Fernet-encrypted secrets (key in `data/secret.key` 0600 or `JOBALERTS_SECRET_KEY`); `.env` wins; status/set/clear for the UI, get for runtime only → verify: tests (round trip, encrypted at rest, env precedence)
- [x] One secrets provider for every key (sources, ntfy, MQTT, Anthropic `api_key=`); keys set in the UI are read at use time (AI scorer rebuilt on save, Phase 19 settings; new aggregator boards start on restart until Phase 21's live reload); log filter redacts secret values and key URL params → verify: tests
- [x] Settings overlay in the DB (personal values encrypted), validated and applied live; Settings page (current situation, household, intent, profile text, resumes, search words, scoring, digest, notifications, API keys write-only with Test, account) → verify: tests + live
- [x] Stay-or-move: stay → jobs that need a move are filtered; open → `pay.move_min_index` pay floor for them; preference line in the AI profile (default `any` = no change for existing installs) → verify: pipeline tests
- [x] Household variants: filing × children under 17 (0–4) × other dependents (0–1) tax tables; 401(k) share applied at runtime by the marginal income-tax slope → verify: 6% case matches today's numbers; tests
- [x] Security checks: canary crawl of every route, MQTT payload, map HTML, logs and raw DB bytes; secret inputs never echo; web package never reads `vault.get`/`os.environ` → verify: tests
- [x] Commit + push Phase 19

## Phase 20 – Jobs page (home)
- [x] `/` tabs (Recommended, Step-ups, Saved, Applied, All recent), filters, job cards; `/jobs/{key}` detail with pay breakdown; save/dismiss/applied (`user_state`); shared row builder with `top_matches` → verify: tests + screenshots
- [x] ntfy alerts open `{web.public_url}/jobs/{key}` (posting link stays as the action button); digest links to `/`; HA attributes get a `link` → verify: tests
- [x] Commit + push Phase 20

## Phase 21 – Boards page
- [x] `/boards` status table (last OK, next poll, listed, matches 7 d, failures, requests/304/429) with summary and auto-refresh → verify: tests + screenshot
- [x] Add (discover), enable/disable, tier, remove; `data/companies.yaml` written atomically; board loops reload live → verify: tests + live add
- [x] Commit + push Phase 21

## Phase 22 – Stories (+ profile, resumes)
- [x] `/stories` list with usage counts; edit form (front matter fields + body), "what the AI sees" preview, create/duplicate/delete (to `.trash`); `dump_story` round trip; safe ids → verify: tests
- [x] Saving rebuilds the scorer live; "re-score recent jobs (≈ $X)" runs in the background with progress → verify: tests + live edit
- [x] Commit + push Phase 22

## Phase 23 – Run anywhere (24/7 and PC on/off)
- [x] Persisted run state: heartbeat, last digest date, catch-up report, aggregator windows from `boards.last_ok_at`, failure-alert and cap notices → verify: tests. ETag cache not persisted: a 304 needs the stored response body (megabytes per board); after a restart each board is fetched once in full, as before
- [x] Catch-up after a gap (start or wake): poll all now, aggregator lookback widened, pre-screen at the end, one summary push + top 3, report banner with the honest gaps, missed digest once → verify: fake-clock tests
- [x] Single-instance lock; autostart files (systemd user unit, Windows Task Scheduler) → verify: tests + docs; Windows script not run (no Windows here)
- [x] Commit + push Phase 23

## Phase 24 – Documentation
- [x] README rewritten for colleagues; `docs/security.md`, `docs/desktop-mode.md`, `docs/architecture.md` complete; `.env.example`, CLAUDE.md; UI alpha checklist → verify: commands in the docs match `--help`
- [x] Commit + push Phase 24

## Phase 25 – Updates keep what's yours; nothing personal without a login (user request 2026-11-26)
- [x] Settings validated value by value on load (config.local.yaml and the Settings page): one renamed, removed or invalid value is set aside and listed on the Settings pages instead of stopping startup or dropping all UI settings; RENAMED table carries values to new names → verify: tests/test_upgrades.py
- [x] Database schema version + automatic backup to data/backups/ before an upgrade changes stored data (last 5 kept); a newer database is left alone → verify: tests
- [x] Personal figures out of pay notes (pushes, digest, Home Assistant, logs; stored notes cleaned in schema 2); share link off until turned on, shared map without housing figures or salary text, exact disclosure on the Account page → verify: tests (guest crawl of every page with planted personal values; shared map)
- [x] docs/upgrading.md, security.md, README → verify: read through
- [x] Commit + push Phase 25

## Phase 26 – Updates from the repository (user request 2026-11-26)
- [x] `updater.py`: check (fetch, commits behind, refuses with local changes to shipped files or local commits), install (legacy-file migration when needed, database backup, fast-forward, reinstall, smoke test with `jobalerts check`, rollback on failure) → verify: tests against temporary git repositories
- [x] Service: `update.mode` off | notify | install, checked once a day (`update.interval_hours`: 24, per the user); notify pushes once per new version and lists it in the digest; install updates and restarts itself (re-exec; exit 75 on Windows for the task's restart) → verify: tests with a fake checker
- [x] Settings → Updates page (version, status, new commits, Check now, Update now, mode); `jobalerts update [--check]`, `jobalerts check`, `jobalerts backup` → verify: tests + live against this repo
- [x] Docker: `tools/update-docker.sh` (fetch, migrate if needed, backup via the container, pull, rebuild, health check, roll back) + systemd timer; the image knows its commit → verify: script dry run with podman
- [x] Docs + commit + push Phase 26

## Phase 27 – Filter page: see and tune what reaches the AI (user request 2026-11-26)
- [x] `/filter`: funnel of every new job by what decided it (age, title words, location, AI first look, pay, scored), click into a group, reason per job; "Score it" on a turned-down job (full score, never pushed) → verify: tests/test_filter_page.py
- [x] Controls on the page: word lists, description excludes; preview of edits against the last 30 days; try a single title; reset; Settings → "What to look for" moved here → verify: tests
- [x] AI-only mode (`criteria.mode: ai`): hard excludes, then every job to the batched first look (titles 50/call, excerpts 8/call), then the full score; separate model per stage (`prescreen.model`, `scoring.model`); runs `prescreen.ai_delay` after new jobs; falls back to word lists without the AI; daily-call estimate on the page → verify: tests
- [x] First-look prompt neutral (no built-in field): framing per mode, the user's field words and `prescreen.guidance` → verify: tests
- [x] Docs + commit + push Phase 27

## Phase 28 – Windows and Mac installers (user request 2026-11-26; unsigned)
- [x] `desktop.py` launcher: app folder per OS, shipped config copied on start, web UI on 127.0.0.1, browser opens with the setup code filled in, tray/menu-bar icon (Open, Start at login, Quit), second start opens the browser, start at login on first run, `--background`, `--self-test` → verify: tests/test_desktop.py; self-test and launch of a PyInstaller build on Linux
- [x] Updates for the desktop app from GitHub Releases: daily check, release notes on Settings → Updates, download checked against SHA256SUMS.txt, silent Windows installer / Mac app swap, restart → verify: tests (mocked downloads)
- [x] `packaging/` (PyInstaller spec, Inno Setup script), `.github/workflows/release.yml` (build, self-test, installer/dmg, release) → verify: Linux build; Windows and Mac only in Actions
- [~] First release built and tried on a real Windows PC and Mac: v0.1.0-rc1 built; Windows and Apple-silicon builds passed their self-test in Actions, the Intel Mac self-test failed (needs user: its log, and a test install)
- [x] docs/install.md, docs/releasing.md, README

## Phase 29 – Multiple users per install (user request 2026-11-26)
- [x] Accounts: `users` table (scrypt), roles admin/user, setup makes the first admin and asks whether users may use the install's Anthropic key; username login limited per address and per name; one-time passwords for new accounts and resets, changed at first login; sessions end on password/role/access changes; first admin can't be removed; `jobalerts users`, `set-password [NAME]` → verify: tests/test_users.py
- [x] Per-user storage (schema 3): `user_jobs`, `ai_calls`, `settings_by_user`, `secrets_by_user`; the single-user data becomes user 1's; old tables left for going back → verify: upgrade test from a single-password database
- [x] Pipeline per user (`Person`): each board polled once, each job through every user's filter, pay index, score, pushes and digest; other users start from the shipped defaults (not config.local.yaml or .env), files in data/users/<id>/; key chain (own key, else the install's when shared, else none) and a shared daily cap → verify: tests
- [x] Web per user: every page the logged-in user's; admin-only users, connections, updates, install keys, board editing; Users page; keys page split into yours / the install's → verify: test_isolation.py (canaries in every personal value, crawled as both users), 403 crawl, live run, screenshots
- [x] Desktop app: `web.lan` lets other computers on the network open it → verify: test
- [x] Docs (security, configuration, upgrading, install, architecture, README)

## User-gated
- [~] Full TheirStack key in `.env` → live call (needs user: full key)
- [~] Adzuna plan decision (needs user)
- [~] Server migration: run `tools/migrate_user_data.sh` on the server before pulling (needs user: server access)

## Progress log
<!-- Append one line per completed task: date – task – how verified. Never write personal data here. -->
2026-11-26 – Phase 27 Filter page + AI-only mode – 325 tests, ruff, screenshots desktop and phone
2026-11-26 – Phase 28 desktop installers – 331 tests; Linux PyInstaller build passes --self-test and a launch; Windows/Mac builds untested until the first Actions run
2026-11-26 – Phase 29 multiple users – 350 tests (isolation crawl, 403 crawl, upgrade), ruff, live run, screenshots desktop and phone
2026-11-26 – Security fixes: ntfy server is the install's (admins), logout ends the session server-side, map links http(s) only, key-test errors to the log, no automatic update installs – 356 tests, ruff, privacy guard
2026-11-26 – Signed releases: tools/sign_release.py, updater requires SHA256SUMS.txt.sig from RELEASE_KEY, release workflow makes drafts, actions pinned to commits – 357 tests, ruff, privacy guard
