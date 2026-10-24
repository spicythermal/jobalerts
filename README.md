# jobalerts

Watches employers' own job boards and pushes newly posted, matching jobs to your phone
within minutes. LinkedIn and Indeed copy listings from those boards hours or days later.

Most companies publish openings first in their applicant tracking system (ATS): Greenhouse,
Lever, Ashby, SmartRecruiters or Workday. Each ATS serves the company's careers page from a
public JSON feed, and jobalerts reads those feeds directly:

```
poll board ─► new job IDs? ─► keyword filter ─► (optional) Claude fit score ─► ntfy push / evening digest
```

- **Fast:** watched companies are checked every 3, 10 or 30 minutes (their tier).
- **Polite:** each request goes to that company's own board, so the load is spread out.
  Requests to the same host are paced, unchanged boards answer `304 Not Modified` (no body),
  `Retry-After` is honored, and failing boards back off exponentially. LinkedIn and Indeed are
  never scraped.
- **Quiet:** the first poll of a board records what's already open without alerting. A board
  that suddenly lists many new matches sends at most 5 pushes; the rest go to the digest.
- **Broad net (optional):** USAJobs, Adzuna and JSearch searches catch companies you don't
  watch. Their matches also feed "hiring for your search, not on your watch list"
  suggestions in the digest.

## Quick start (local, Windows or Linux)

```sh
python -m venv .venv            # on Windows, keep it outside Nextcloud-synced folders
.venv/bin/pip install -e .[dev] # Windows: .venv\Scripts\pip install -e .[dev]
cp .env.example .env            # fill in CONTACT_EMAIL and NTFY_TOPIC at least
jobalerts poll --dry-run        # fetch + filter every board, store nothing, send nothing
```

Then edit the config (next section), and run `jobalerts test-notify` to check your phone.

## Configure

| File | What goes in it |
|---|---|
| `.env` | Secrets: `NTFY_TOPIC`, `ANTHROPIC_API_KEY`, aggregator keys. Never committed. |
| `config/config.yaml` | Search criteria, polling tiers, scoring thresholds, digest time, aggregators. |
| `config/companies.yaml` | The watch list, generated from `config/candidates.txt`. |
| `config/candidates.txt` | Companies to resolve into the watch list, one per line (see below). |
| `resume_reference/` | Your resumes (PDF, `.md` or `.txt`). AI scoring compares every posting against all of them. Committed (private repo) and mounted read-only into the container. |
| `config/profile.md` | What the resumes don't say: target roles, location, salary floor, deal-breakers. |

**Criteria** (`config.yaml` → `criteria`) are case-insensitive whole-word phrases.
`title_include` must match. `title_exclude`, `seniority_exclude` and `description_exclude`
reject a job. `locations` must match unless the job is remote and `allow_remote: true`.

**Adding companies:**

```sh
jobalerts discover "Cloudflare" --add                      # probes Greenhouse/Lever/Ashby/SmartRecruiters
jobalerts discover https://company.com/careers --add       # finds ATS links on a careers page (and Workday URLs)
jobalerts discover "Tailscale" --add --tier priority
jobalerts discover --file config/candidates.txt --add      # a whole list; already-listed boards are skipped
```

`candidates.txt` takes `Company Name` or `Company Name | https://careers.url` per line. Companies
on Taleo, iCIMS, SuccessFactors or in-house career sites aren't supported and are listed as
not found at the end of the run; the aggregators still catch many of their postings.

For Workday and SmartRecruiters, add `search:` to the entry so big enterprise boards stay
small (see the comments in `companies.yaml`).

**Phone:** install the ntfy app (Android/iOS) and subscribe to your `NTFY_TOPIC` on
`ntfy.sh`. Pick a long random topic name, since anyone who knows it can read it.
A self-hosted ntfy works too: set `notify.server` and `NTFY_TOKEN`.

**AI scoring:** put your resumes in `resume_reference/`, fill in `config/profile.md`, set
`ANTHROPIC_API_KEY`, and set `scoring.enabled: true`. Keyword matches are scored 0–100 by
Claude Haiku 5.5 against the resumes and profile (about $0.001 each with two resumes). Each
alert says which resume version to apply with ("Apply with: …"), and so does the digest.
A score of 75 or more is pushed now, 50–74 goes to the evening digest, and anything
lower is only stored. If scoring fails or the daily cap is reached, keyword matches are
still pushed. Preview scores before turning it on:

```sh
jobalerts poll --dry-run --score 5
```

## Commands

```
jobalerts [-c CONFIG] [-v] {run,poll,discover,test-notify,digest,stats}

  run                  the service: poll every board on its tier interval, forever
  poll [--once] [--dry-run] [--board TEXT] [--score N]
                       one pass over every board (--dry-run stores and sends nothing)
  discover [TARGET ...] [--file FILE] [--add] [--tier TIER] [--name NAME]
                       find companies' ATS boards by name, careers URL, or a list file
  test-notify          send a sample alert to your ntfy topic
  digest [--dry-run]   send the daily digest now
  stats                board health, request rates, detection delay, AI spend
```

`-c` defaults to `config/config.yaml` (or `$JOBALERTS_CONFIG`). `$JOBALERTS_DB` overrides the
database path.

## Deploy on the homelab (Docker)

```sh
git clone <this repo> jobalerts && cd jobalerts
cp .env.example .env && nano .env        # secrets
nano config/config.yaml config/companies.yaml config/profile.md
mkdir -p resume_reference && cp ~/resumes/*.pdf resume_reference/   # mounted read-only
mkdir -p data && sudo chown 1000:1000 data config   # the container runs as uid 1000
docker compose up -d --build
docker compose logs -f                   # first polls record silent baselines
docker compose exec jobalerts jobalerts stats
docker compose exec jobalerts jobalerts discover "Some Company" --add && docker compose restart
```

Set `notify.timezone` so the digest arrives at your local `digest_time`. Restart the
container after any config change.

## Living with it

- **Daily digest** (default 18:00) lists matches that weren't pushed, companies hiring for
  your search that you don't watch, and 24 h health (jobs seen, pushes, request count,
  rate limits, failing boards). It's sent even on quiet days, so silence never means broken.
- **Health pushes:** a board failing 5 times in a row (bad slug, site change) sends one
  alert, and another when it recovers. A source missing its API key is disabled with an alert.
- **`jobalerts stats`** shows detection delay (first seen minus published) and per-host
  request and 304 counts. Use it to tune tiers.

## Limitations

- Companies on iCIMS, Taleo or SuccessFactors have no clean public feed. Use the aggregators
  or watch those companies manually.
- Workday reports "Posted 3 Days Ago", not an exact time, until a job is enriched.
- An ATS sometimes reposts an old job with a new ID. Jobs published more than
  `max_age_days` ago are never alerted.

## Development

```sh
pip install -e .[dev] && pytest
```

Adapters live in `src/jobalerts/sources/`, one module per ATS or aggregator, each with a
fixture test in `tests/test_sources.py`.
