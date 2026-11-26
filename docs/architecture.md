# Architecture

jobalerts is one Python process (`jobalerts run`) over one SQLite database. It polls employers'
own job boards, filters and AI-scores new postings, prices their pay against the cost of living,
and tells you about the good ones: phone pushes (ntfy), an evening digest, Home Assistant sensors
(MQTT), and a web UI (jobs, boards, cost-of-living map, stories, settings) served by the same
process. It runs the same way on a 24/7 server and on a PC that's only on sometimes (catch-up).

## Data flow

```
boards (ATS feeds, aggregator searches)
  └─ scheduler._board_loop ── http.HttpClient (per-host pacing, ETag/304, Retry-After, backoff)
       └─ sources/<ats>.py ── normalized models.Job
            └─ Runner.poll_board / _process
                 ├─ store: new job? (first poll of a board = silent baseline)
                 ├─ filters.JobFilter: title / seniority / location / age   ─ rejected ─► prescreen queue
                 ├─ col.PayIndex: stated pay → take-home (taxes.py) ÷ local prices → pay gate
                 ├─ scorer.Scorer (Claude): fit 0–100, step-up, red flags, best resume, top 3 stories
                 └─ route: push (notify.py) │ digest │ stored
  dashboard loop ─► MQTT sensors (dashboard.py) and the map page (colmap.py)
  daily loop ─► cost-of-living data refresh (col.build_data) and the digest
  heartbeat loop ─► after a gap (start or wake): catch_up() polls everything once, one summary push
  web UI (web/, uvicorn task) ─► reads the store; Settings/Boards/Stories edits call
                                 Runner.reconfigure / reload_watchlist (no restart)
```

## Modules (`src/jobalerts/`)

| Module | Role |
|---|---|
| `cli.py` | Commands: run, web, set-password, poll, discover, test-notify, digest, top, stats, backfill, map, pay |
| `config.py` | pydantic config; loads `config/config.yaml`, then `data/config.local.yaml` on top, then the watch list |
| `userdata.py` | The data folder, shipped examples, migrating an old install |
| `scheduler.py` | `Runner`: the pipeline and the polling, digest, pre-screen and dashboard loops |
| `store.py` | SQLite: jobs, boards, AI usage, suggestions, per-host stats; additive migrations |
| `http.py` | Polite async HTTP client |
| `sources/` | One adapter per ATS (Greenhouse, Lever, Ashby, SmartRecruiters, Workday, UKG, Personio, Workable, BambooHR, Rippling, Breezy, SuccessFactors) and aggregator (USAJobs, Adzuna, JSearch, TheirStack) |
| `filters.py` | Keyword prefilter (plurals, wildcards, field words beat soft excludes) |
| `prescreen.py` | Cheap Claude second look at titles the keywords turned down |
| `scorer.py` | Claude fit scoring against work stories (or resumes) and the profile |
| `stories.py`, `resumes.py` | Read work stories (Markdown + front matter) and resumes (PDF/MD/TXT) |
| `col.py` | Cost-of-living data build (BEA, HUD, Zillow, Freddie Mac, World Bank, Census) and the pay index (rent and buy, move vs stay) |
| `taxes.py` | Take-home pay from the PolicyEngine-built tax table + local taxes + rates abroad |
| `colmap.py`, `map_template.html` | The cost-of-living map page |
| `web/` | FastAPI app: `app.py` (login, sessions, CSRF, headers, map), `jobs_pages.py`, `boards_pages.py`, `stories_pages.py`, `settings_pages.py`, Jinja templates, vendored htmx |
| `vault.py` | Encrypted API keys (write-only from the UI), `secret()` for the service, log redaction |
| `settings.py` | Settings saved on the web UI, layered on the config files (personal values encrypted) |
| `runstate.py` | Heartbeat, catch-up report, missed digest, single-instance lock |
| `watchlist.py` | Edits `companies.yaml` from the Boards page, keeping comments |
| `dashboard.py` | Top matches and AI cost; MQTT publisher for Home Assistant |
| `notify.py` | ntfy pushes |
| `discover.py` | Find which ATS a company uses |

## Files

| Where | What | Tracked |
|---|---|---|
| `config/config.yaml` | Shipped defaults | yes |
| `config/taxes.yaml`, `config/tax_data/`, `config/col_data.json` | Tax rules, tax tables per household, price data seed | yes |
| `data/config.local.yaml` | Your settings (only what differs from the defaults) | no |
| `data/profile.md`, `data/stories/`, `data/resumes/`, `data/companies.yaml` | Your profile, stories, resumes, watch list (seeded from `src/jobalerts/examples/`) | no |
| `data/jobalerts.db` | Everything seen, board health, AI usage, settings, encrypted keys | no |
| `data/secret.key` | The key for the encrypted keys and settings, and web sessions | no |
| `.env` | Secrets | no |

## Database (`store.py`)

- `jobs`: one row per posting key (`source:board:id`). Status follows the pipeline (baseline,
  duplicate, stale, closed, filtered, prescreen, low_pay, low, digest, alerted, digested,
  notify_failed), plus score, step-up, stories, pay indexes and the note shown in alerts.
- `boards`: first poll, last OK, last count, failures and the last error, per board.
- `ai_usage`: Claude calls and tokens per UTC day (daily cap and cost sensor).
- `suggestions`: companies the aggregators keep finding that you don't watch.
- `host_stats`: requests, 304s, 429/503s and errors per host per day.
- `meta`: small named values: web login (password hash, version, share token), heartbeat, last
  digest date, catch-up report, failure-alert and AI-cap notices.
- `secrets`: API keys saved on the Settings page (Fernet ciphertext).
- `settings`: values saved on the Settings page (JSON; personal ones as ciphertext).
- `jobs.user_state`: saved / dismissed / applied from the jobs page.

Config layers, lowest first: `config/config.yaml` → `data/config.local.yaml` → the `settings`
table. `Config.value_sources` records which layer each value came from.

## Conventions

Python 3.12, `src/` layout, httpx async, pydantic, PyYAML, sqlite3, pytest. Personal data never
goes in tracked files. Secrets come from `.env` or the encrypted vault (`vault.secret()`). Never
scrape LinkedIn or Indeed; respect per-host rate limits.
