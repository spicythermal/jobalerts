# Architecture

One Python process (`jobalerts run`) and one SQLite database. The process polls job boards, filters
and scores new jobs, computes pay indexes, sends pushes (ntfy), the digest and Home Assistant
sensors (MQTT), and serves the web UI.

## Data flow

```
boards (ATS feeds, aggregator searches)
  └─ scheduler._board_loop ── http.HttpClient (per-host pacing, ETag/304, Retry-After, backoff)
       └─ sources/<ats>.py ── normalized models.Job
            └─ Runner.poll_board / _process
                 ├─ store: new job? (first poll of a board = silent baseline)
                 ├─ filters.JobFilter: title / seniority / location / age   ─ rejected ─► prescreen queue
                 │    (criteria.mode "ai": only exclude/seniority/age; every other job ─► prescreen queue)
                 ├─ col.PayIndex: stated pay → take-home (taxes.py) ÷ local prices → pay gate
                 ├─ scorer.Scorer (Claude): fit 0–100, step-up, red flags, best resume, top 3 stories
                 └─ route: push (notify.py) │ digest │ stored
  dashboard loop ─► MQTT sensors (dashboard.py) and the map page (colmap.py)
  daily loop ─► cost-of-living data refresh (col.build_data) and the digest
  heartbeat loop ─► after a gap (start or wake): catch_up() polls everything once, one summary push
  update loop ─► daily repository check (updater.py)
  web UI (web/, uvicorn task) ─► reads the store; edits call Runner.reconfigure / reload_watchlist
```

## Modules (`src/jobalerts/`)

| Module | Role |
|---|---|
| `cli.py` | Commands (see README) |
| `config.py` | pydantic config; loads `config/config.yaml`, then `data/config.local.yaml` on top, then the watch list |
| `userdata.py` | The data folder, shipped examples, migrating an old install |
| `scheduler.py` | `Runner`: the pipeline and the polling, digest, pre-screen and dashboard loops |
| `store.py` | SQLite tables, schema version, backups |
| `http.py` | Async HTTP client with per-host pacing |
| `sources/` | One adapter per ATS (Greenhouse, Lever, Ashby, SmartRecruiters, Workday, UKG, Personio, Workable, BambooHR, Rippling, Breezy, SuccessFactors) and aggregator (USAJobs, Adzuna, JSearch, TheirStack) |
| `filters.py` | Word-list filter; funnel groups for the Filter page |
| `prescreen.py` | Batched Claude check of titles, then excerpts, before the full score |
| `scorer.py` | Claude fit and step-up scores against stories (or resumes) and the profile |
| `stories.py`, `resumes.py` | Read work stories (Markdown + front matter) and resumes (PDF/MD/TXT) |
| `col.py` | Price data build (BEA, HUD, Zillow, Freddie Mac, World Bank, Census) and the pay index |
| `taxes.py` | Take-home pay from the tax tables, local taxes and rates abroad |
| `colmap.py`, `map_template.html` | Map page |
| `web/` | FastAPI app: `app.py` (login, sessions, CSRF, headers, map), `jobs_pages.py`, `boards_pages.py`, `filter_pages.py`, `stories_pages.py`, `settings_pages.py`, Jinja templates, vendored htmx |
| `vault.py` | Encrypted API keys, `secret()` for the service, log redaction |
| `settings.py` | Web UI settings, stored over the config files |
| `runstate.py` | Heartbeat, catch-up report, missed digest, single-instance lock |
| `watchlist.py` | Edits `companies.yaml` from the Boards page, keeping comments |
| `dashboard.py` | Top matches and AI cost; MQTT publisher for Home Assistant |
| `notify.py` | ntfy pushes |
| `discover.py` | Find a company's ATS board |
| `updater.py` | Repository check, install, rollback |

## Files

| Where | What | Tracked |
|---|---|---|
| `config/config.yaml` | Shipped defaults | yes |
| `config/taxes.yaml`, `config/tax_data/`, `config/col_data.json` | Tax rules, tax tables per household, price data seed | yes |
| `data/config.local.yaml` | Your settings (only what differs from the defaults) | no |
| `data/profile.md`, `data/stories/`, `data/resumes/`, `data/companies.yaml` | Your profile, stories, resumes, watch list (seeded from `src/jobalerts/examples/`) | no |
| `data/jobalerts.db` | Jobs, board health, AI usage, settings, encrypted keys | no |
| `data/secret.key` | Encryption and session-signing key | no |
| `.env` | Secrets | no |

## Database (`store.py`)

- `jobs`: one row per posting key (`source:board:id`). Status follows the pipeline (baseline,
  duplicate, stale, closed, filtered, prescreen, low_pay, low, digest, alerted, digested,
  notify_failed), plus score, step-up, stories, pay indexes and the note shown in alerts.
- `boards`: first poll, last OK, last count, failures and the last error, per board.
- `ai_usage`: Claude calls and tokens per UTC day (daily cap and cost sensor).
- `suggestions`: unwatched companies found by the aggregators.
- `host_stats`: requests, 304s, 429/503s and errors per host per day.
- `meta`: password hash and version, share token, heartbeat, last digest date, catch-up report,
  notice state, schema version.
- `secrets`: API keys saved on the Settings page (Fernet ciphertext).
- `settings`: values saved on the Settings page (JSON; personal ones as ciphertext).
- `jobs.user_state`: saved, dismissed or applied.

Config layers, lowest first: `config/config.yaml` → `data/config.local.yaml` → the `settings`
table. `Config.value_sources` records which layer each value came from.

## Conventions

Python 3.12, `src/` layout, httpx async, pydantic, PyYAML, sqlite3, pytest. Personal data never
goes in tracked files. Secrets come from `.env` or the encrypted vault (`vault.secret()`). Never
scrape LinkedIn or Indeed; respect per-host rate limits.
