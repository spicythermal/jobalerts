# jobalerts

Watches employers' own job boards and tells you about new, matching jobs within minutes, scored
by AI against your own work stories and priced against the cost of living where the job is.
LinkedIn and Indeed copy listings from those boards hours or days later.

Most companies publish openings first in their applicant tracking system (ATS): Greenhouse,
Lever, Ashby, SmartRecruiters, Workday, UKG/UltiPro, Personio, Workable, BambooHR, Rippling,
Breezy or SuccessFactors. Each serves its careers page from a public feed, and jobalerts reads
those feeds directly:

```
poll board ─► new jobs? ─► keyword filter ─► pay index ─► Claude fit score ─► push / digest / web UI
```

- **Fast:** watched companies are checked every 3, 10 or 30 minutes.
- **Polite:** requests to one host are paced, unchanged boards answer `304 Not Modified`,
  `Retry-After` is honored and failing boards back off. LinkedIn and Indeed are never scraped.
- **Quiet:** the first poll of a board only records what's open. A board that suddenly lists
  many matches sends at most 5 pushes; the rest go to the evening digest.
- **Scored against you:** each match gets a 0–100 fit from Claude against your work stories and
  profile, a step-up score for credible promotions, and the three stories that fit it best.
- **Priced:** a pay index compares take-home pay against local prices (rent or buy) with what you
  have now, for your household. Stay, open to moving, or anywhere: you choose how moves count.
- **Yours:** a web UI (jobs, boards, map, stories, settings) behind a login, and optionally
  phone pushes (ntfy) and Home Assistant sensors.
- **Shared:** several people can use one install, each with their own profile, stories, filter,
  scores and phone pushes. Boards are polled once for all.
- **On all the time or not:** run it on a home server, or on a PC that's only on sometimes; after
  a gap it catches up by itself.

GPL-3.0. Personal data (your settings, profile, stories, resumes, watch list) stays in the
`data/` folder on your machine, which git ignores.

## Quick start

**Windows or Mac app:** download the installer from the
[latest release](https://github.com/spicythermal/jobalerts/releases/latest). Steps:
[docs/install.md](docs/install.md).

**Docker:**

```sh
git clone https://github.com/spicythermal/jobalerts.git jobalerts && cd jobalerts
mkdir -p data && docker compose up -d --build
docker compose logs jobalerts | grep "setup code"
```

**Python 3.11+** (Windows, macOS, Linux):

```sh
git clone https://github.com/spicythermal/jobalerts.git jobalerts && cd jobalerts
python -m venv .venv && .venv/bin/pip install -e .     # Windows: .venv\Scripts\pip install -e .
.venv/bin/jobalerts run                                 # prints the setup code
```

Open `http://<this machine>:8765/setup`, enter the setup code, and make the admin account. The first
start creates `data/` with an example profile, two example stories and a 10-company watch list.

## Web UI

- **Jobs:** the home page. Tabs: Recommended, Step-ups, Saved, Applied, All recent. Each job's page
  shows the fit summary, red flags, matching stories, the resume to use and the pay index
  calculation. Save, dismiss or mark applied.
- **Boards:** each watched company and aggregator search: status, next poll, jobs listed, recent
  matches. Add a company by name or careers URL; turn boards on or off; change the poll interval;
  remove boards.
- **Filter:** counts of new jobs by what decided them, and the reason for each job turned down.
  "Score it" scores a turned-down job. Settings: keywords first (word lists decide; the AI checks
  what they turn down) or AI only (all new jobs go to the AI: titles in batches, then excerpts, then
  a full score); the model for each stage; guidance for the AI; the word lists. Preview edits
  against the last 30 days or test a single title before saving.
- **Map:** US metros colored by pay index for a salary, renting or buying, with your top jobs.
- **Stories:** edit work stories and preview what the AI reads. Re-score recent jobs after a change.
- **Settings:** your situation (salary, home, housing cost, household, stay or move), scoring and
  alerts, API keys (write-only), profile and resumes, password and the Home Assistant map link. Admins
  also: connections and Home Assistant, updates, and users.

Each user sees their own jobs, scores, filter, stories, profile and settings. Admins add users on
Settings → Users and choose whether users may use the install's Anthropic key
([security](docs/security.md#users)).

## Setup

1. **Settings → Your situation:** salary, location, monthly housing cost, household, stay or move.
2. **Settings → Profile and resumes:** what you want and your deal-breakers; upload resumes.
3. **Stories:** one story per project you'd describe in an interview.
4. **Filter:** field and role words for job titles, or AI only.
5. **Boards:** add companies.
6. **Settings → API keys:** an [Anthropic](https://console.anthropic.com) key for AI scoring (about
   $0.001 per job) and an [ntfy](https://ntfy.sh) topic for phone pushes. Optional: USAJobs, Adzuna,
   JSearch, TheirStack.
7. **Settings → Connections:** this machine's address as your phone reaches it, so alerts link here.

## Server or desktop

There is no mode setting. When it starts after more than 45 minutes off (or wakes from sleep), it
polls every board, searches the aggregators back over the gap, and sends one summary push plus the
top three jobs. The rest are on the Jobs page and in the digest. A missed digest is sent once. Jobs
posted and removed while it was off are not seen.

- Server: [docs/deploy.md](docs/deploy.md)
- Desktop, starting at login: [docs/desktop-mode.md](docs/desktop-mode.md)

One `jobalerts run` per database.

## Files

| Where | What |
|---|---|
| `data/` | Your data: database, settings, key store and its key (`secret.key`), profile, stories, resumes, watch list, `config.local.yaml`. Back it up. |
| `config/` | Shipped defaults and data tables. Override on the Settings page or in `data/config.local.yaml`. |
| `.env` | Optional API keys as environment variables; these override keys saved in Settings. |

More: [configuration](docs/configuration.md), [security](docs/security.md),
[architecture](docs/architecture.md).

## Commands

```
jobalerts [-c CONFIG] [-v] {run,web,set-password,users,update,check,backup,poll,discover,test-notify,digest,top,stats,backfill,map,pay}

  run                  the service: polls every board on its interval, with the web UI on port 8765
  web                  the web UI only (no polling or pushes), over the same database
  set-password [NAME]  set or reset a web UI password (default: the first admin's)
  users {list,add,reset,admin,user,disable,enable,delete} [NAME] [--admin]
                       manage web UI accounts (add and reset print a one-time password)
  poll [--dry-run] [--board TEXT] [--score N]
                       one pass over every board (--dry-run stores and sends nothing)
  discover [TARGET ...] [--file FILE] [--add] [--tier TIER] [--name NAME]
                       find companies' job boards by name, careers URL or a list file
  test-notify          send a sample alert to your ntfy topic
  digest [--dry-run] [--user NAME]
                       send the daily digest now
  top [-n N] [--days D] [--push] [--user NAME]
                       best recent matches; --push also updates the Home Assistant sensor
  stats                board health, request rates, detection delay, AI spend
  backfill [--dry-run] [--push] [--days N] [--rescore]
                       re-check pay indexes, AI-score jobs that were open at the first poll;
                       --rescore redoes scores made before your stories or profile changed
  pay [SALARY LOCATION] [--remote] [--update]
                       explain the pay index for a salary and place; --update refreshes price data
  map [--out FILE]     write the cost-of-living map to a file (default data/map.html)
  update [--check]     install the newest version from the git repository (or just show it)
  check                check that this version starts with your settings and database
  backup               copy the database to data/backups/
```

`-c` defaults to `config/config.yaml` (or `$JOBALERTS_CONFIG`); `$JOBALERTS_DB` overrides the
database path.

## Updating

- Windows or Mac app: Settings → Updates.
- Python install: checks the repository daily and tells you; Settings → Updates or
  `jobalerts update` installs.
- Docker: `tools/update-docker.sh` on the host; `contrib/jobalerts-update.timer` runs it daily.
- By hand: `git pull`, then `docker compose up -d --build` or `pip install -e .` and restart.

Updates don't change `data/`. See [docs/upgrading.md](docs/upgrading.md).

### Installs from before 2026-11-24

Move your files into `data/` before pulling:

```sh
git fetch && git show origin/main:tools/migrate_user_data.sh | sh
git pull && docker compose up -d --build
```

The script doesn't overwrite files in `data/`. On the next start, the values you changed in the
old `config.yaml` move to `data/config.local.yaml`. The Home Assistant map card needs the new share
link from Settings → Account.

### Installs cloned from the earlier repository

`git pull` can't switch to the GitHub history. On a Docker install, run:

```sh
git fetch https://github.com/spicythermal/jobalerts.git main && git show FETCH_HEAD:tools/switch-to-public.sh | sh
```

It archives the folder next to itself, copies your files into `data/` and verifies them, switches
the code, rebuilds, and runs `jobalerts check`. If the check fails, it restores the old code and
prints how to restore the archive.

## Limitations

- iCIMS, Taleo, Eightfold and in-house career sites have no public feed; aggregator searches find
  some of their jobs.
- BambooHR and Rippling lists have no posting date until a job is fetched in full; Workday gives
  "Posted 3 Days Ago".
- The pay index uses base pay only (no bonus, equity or benefits) and assumes you live in the job's
  city. Taxes abroad are approximate (±5 points); prices abroad are country averages.
- An ATS sometimes reposts an old job with a new ID. Jobs older than `max_age_days` are never alerted.

## Development

```sh
pip install -e .[dev] && pytest && ruff check src tests tools
git config core.hooksPath .githooks      # privacy guard for commits and pushes
```

Keep personal data out of commits; it belongs in `data/`. The guard checks commits against your
terms in `~/.config/jobalerts/private-terms.txt` (not committed). CI checks for personal files and
credentials.

Sources: `src/jobalerts/sources/`, one module per ATS or aggregator, tested in
`tests/test_sources.py`. Web UI: `src/jobalerts/web/` (FastAPI, Jinja, htmx). Plan: `PLAN.md`;
conventions: [CLAUDE.md](CLAUDE.md). Desktop app builds and releases: [docs/releasing.md](docs/releasing.md).
