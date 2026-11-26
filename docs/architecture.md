# Architecture

One Python process (`jobalerts run`) and one SQLite database. The process polls job boards, filters
and scores new jobs, computes pay indexes, sends pushes (ntfy), the digest and Home Assistant
sensors (MQTT), and serves the web UI.

Each board is polled once. Each new job then goes through every user's pipeline (`scheduler.Person`):
their filter, pay index, AI score, routing, pushes and digest, written to their own rows.

## Data flow

```
boards (ATS feeds, aggregator searches)
  └─ scheduler._board_loop ── http.HttpClient (per-host pacing, ETag/304, Retry-After, backoff)
       └─ sources/<ats>.py ── normalized models.Job
            └─ Runner.poll_board: new job? (first poll of a board = silent baseline); duplicate?
               └─ for each user: Person.process (their settings, their store.user(uid) rows)
                 ├─ filters.JobFilter: title / seniority / location / age   ─ rejected ─► prescreen queue
                 │    (criteria.mode "ai": only exclude/seniority/age; every other job ─► prescreen queue)
                 ├─ col.PayIndex: stated pay → take-home (taxes.py) ÷ local prices → pay gate
                 ├─ scorer.Scorer (Claude): fit 0–100, step-up, red flags, best resume, top 3 stories
                 └─ route: push (notify.py) │ digest │ stored
  dashboard loop ─► MQTT sensors (dashboard.py) and the map page (colmap.py)
  daily loop ─► cost-of-living data refresh (col.build_data) and each user's digest at their time
  heartbeat loop ─► after a gap (start or wake): catch_up() polls everything once, one summary push
  update loop ─► daily repository check (updater.py)
  web UI (web/, uvicorn task) ─► the logged-in user's Person; edits call Runner.refresh / reload_watchlist
```

## Modules (`src/jobalerts/`)

| Module | Role |
|---|---|
| `cli.py` | Commands (see README) |
| `config.py` | pydantic config; loads `config/config.yaml`, then `data/config.local.yaml` on top, then the watch list |
| `userdata.py` | The data folder, shipped examples, migrating an old install |
| `scheduler.py` | `Runner`: polling and the loops; `Person`: one user's pipeline (filter, pay, score, route, push, digest, map) |
| `store.py` | SQLite tables, schema version, backups; `UserJobs`: one user's view of the jobs |
| `users.py` | Accounts: create, authenticate, roles, one-time passwords, delete |
| `http.py` | Async HTTP client with per-host pacing |
| `sources/` | One adapter per ATS (Greenhouse, Lever, Ashby, SmartRecruiters, Workday, UKG, Personio, Workable, BambooHR, Rippling, Breezy, SuccessFactors) and aggregator (USAJobs, Adzuna, JSearch, TheirStack) |
| `filters.py` | Word-list filter; funnel groups for the Filter page |
| `prescreen.py` | Batched Claude check of titles, then excerpts, before the full score |
| `scorer.py` | Claude fit and step-up scores against stories (or resumes) and the profile |
| `stories.py`, `resumes.py` | Read work stories (Markdown + front matter) and resumes (PDF/MD/TXT) |
| `col.py` | Price data build (BEA, HUD, Zillow, Freddie Mac, World Bank, Census) and the pay index |
| `taxes.py` | Take-home pay from the tax tables, local taxes and rates abroad |
| `colmap.py`, `map_template.html` | Map page |
| `web/` | FastAPI app: `app.py` (login, sessions, CSRF, admin-only paths, headers, map), `jobs_pages.py`, `boards_pages.py`, `filter_pages.py`, `stories_pages.py`, `settings_pages.py`, `users_pages.py`, Jinja templates, vendored htmx |
| `vault.py` | Encrypted API keys (the install's and each user's), `secret()` for the service, log redaction |
| `settings.py` | Web UI settings, stored over the config files; each section the install's or each user's |
| `runstate.py` | Heartbeat, catch-up report, missed digest, single-instance lock |
| `watchlist.py` | Edits `companies.yaml` from the Boards page, keeping comments |
| `dashboard.py` | Top matches and AI cost; MQTT publisher for Home Assistant |
| `notify.py` | ntfy pushes |
| `discover.py` | Find a company's ATS board |
| `updater.py` | Updates: from the git repository (checkout), or from GitHub Releases (desktop app) |
| `desktop.py` | The desktop app's launcher: app folder, local-only web UI, browser, tray icon, start at login |

## Files

| Where | What | Tracked |
|---|---|---|
| `config/config.yaml` | Shipped defaults | yes |
| `config/taxes.yaml`, `config/tax_data/`, `config/col_data.json` | Tax rules, tax tables per household, price data seed | yes |
| `data/config.local.yaml` | Your settings (only what differs from the defaults) | no |
| `data/profile.md`, `data/stories/`, `data/resumes/`, `data/companies.yaml` | The first admin's profile, stories, resumes; the watch list (seeded from `src/jobalerts/examples/`) | no |
| `data/users/<id>/` | Other users' profile, stories, resumes | no |
| `data/jobalerts.db` | Jobs, board health, AI usage, settings, encrypted keys | no |
| `data/secret.key` | Encryption and session-signing key | no |
| `.env` | Secrets | no |

## Database (`store.py`)

- `jobs`: one row per posting key (`source:board:id`): what the board says, and a shared status
  (baseline, duplicate or seen). The description is kept only while someone's pre-screen waits.
- `user_jobs`: one row per user and job: status in that user's pipeline (stale, closed, filtered,
  prescreen, low_pay, low, digest, alerted, digested, notify_failed), score, step-up, stories, pay
  indexes, the note shown in alerts, and saved / dismissed / applied. `Store.user(uid)` reads both.
- `users`: username, scrypt hash, role, a version that ends sessions, must-change, disabled.
- `boards`: first poll, last OK, last count, failures and the last error, per board.
- `ai_calls`: Claude calls and tokens per UTC day and user, and the calls made on the install's key
  for others (daily caps, cost).
- `suggestions`: unwatched companies found by the aggregators.
- `host_stats`: requests, 304s, 429/503s and errors per host per day.
- `meta`: heartbeat, update status, failing boards; per user (`user.<id>.…`, user 1 without the
  prefix): last digest date, catch-up report, cap notice, share token.
- `secrets_by_user`: API keys saved on the Settings page (Fernet ciphertext); user 0 = the install.
- `settings_by_user`: values saved on the Settings page (JSON; personal ones as ciphertext); user 0 =
  the install.
- `ai_usage`, `secrets`, `settings` and the per-user columns of `jobs`: the single-user layout before
  schema 3, copied to the tables above once and left as they were.

Config layers, lowest first: `config/config.yaml` → `data/config.local.yaml` (the first admin's; other
users start from `config/config.yaml` for their own sections) → the install's settings → the user's.
`Config.value_sources` records which layer each value came from.

## Desktop app

`packaging/`: PyInstaller spec and launcher, Inno Setup script. `.github/workflows/release.yml` builds,
self-tests and publishes them on version tags ([releasing.md](releasing.md)). The app copies the shipped
`config/` into its folder on each start and keeps `data/` beside it, so the same relative paths work.

## Conventions

Python 3.12, `src/` layout, httpx async, pydantic, PyYAML, sqlite3, pytest. Personal data never
goes in tracked files. Secrets come from `.env` or the encrypted vault (`vault.secret()`). Never
scrape LinkedIn or Indeed; respect per-host rate limits.
