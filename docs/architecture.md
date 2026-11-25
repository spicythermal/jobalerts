# Architecture

jobalerts is one Python process (`jobalerts run`) over one SQLite database. It polls employers'
own job boards, filters and AI-scores new postings, prices their pay against the cost of living,
and tells you about the good ones: phone pushes (ntfy), an evening digest, Home Assistant sensors
(MQTT) and a cost-of-living map. Work in progress (see PLAN.md): a native web UI (jobs, boards,
map, stories, settings), a secret vault, and catch-up runs for PCs that aren't on all the time.

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
  dashboard loop ─► MQTT sensors (dashboard.py) and the map page (colmap.py, mapserver.py)
  daily loop ─► cost-of-living data refresh (col.build_data) and the digest
```

## Modules (`src/jobalerts/`)

| Module | Role |
|---|---|
| `cli.py` | Commands: run, poll, discover, test-notify, digest, top, stats, backfill, map, pay |
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
| `colmap.py`, `map_template.html`, `mapserver.py` | The cost-of-living map page and its server |
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
| `data/jobalerts.db` | Everything seen, board health, AI usage | no |
| `.env` | Secrets | no |

## Database (`store.py`)

- `jobs`: one row per posting key (`source:board:id`). Status follows the pipeline (baseline,
  duplicate, stale, closed, filtered, prescreen, low_pay, low, digest, alerted, digested,
  notify_failed), plus score, step-up, stories, pay indexes and the note shown in alerts.
- `boards`: first poll, last OK, last count, failures and the last error, per board.
- `ai_usage`: Claude calls and tokens per UTC day (daily cap and cost sensor).
- `suggestions`: companies the aggregators keep finding that you don't watch.
- `host_stats`: requests, 304s, 429/503s and errors per host per day.

## Conventions

Python 3.12, `src/` layout, httpx async, pydantic, PyYAML, sqlite3, pytest. Personal data never
goes in tracked files. Secrets come from `.env` (and, from Phase 19, the encrypted vault). Never
scrape LinkedIn or Indeed; respect per-host rate limits.
