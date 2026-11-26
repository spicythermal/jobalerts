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
- **Yours:** a web UI (jobs, boards, map, stories, settings) behind a password, and optionally
  phone pushes (ntfy) and Home Assistant sensors.
- **On all the time or not:** run it on a home server, or on a PC that's only on sometimes; after
  a gap it catches up by itself.

GPL-3.0. Personal data (your settings, profile, stories, resumes, watch list) stays in the
`data/` folder on your machine, which git ignores.

## Quick start

**Docker** (a home server or a desktop with Docker):

```sh
git clone https://github.com/spicythermal/jobalerts.git jobalerts && cd jobalerts
mkdir -p data && docker compose up -d --build
docker compose logs jobalerts | grep "setup code"
```

**Python 3.11+** (Windows, macOS or Linux):

```sh
git clone https://github.com/spicythermal/jobalerts.git jobalerts && cd jobalerts
python -m venv .venv && .venv/bin/pip install -e .     # Windows: .venv\Scripts\pip install -e .
.venv/bin/jobalerts run                                 # prints the setup code, then keeps running
```

Then open `http://<this machine>:8765/setup`, enter the setup code from the log and choose a
password. On first start jobalerts creates `data/` with an example profile, two example stories
and a 10-company watch list; replace them with your own on the web UI.

## The web UI

- **Jobs** (the home page, and where alerts land): Recommended, Step-ups, Saved, Applied and All
  recent, with filters; each job has its own page with the fit summary, red flags, the stories
  that fit it, the resume to apply with and how its pay index was worked out. Save, dismiss or
  mark applied.
- **Boards:** every watched company and aggregator search, polling fine or failing, when it runs
  next, jobs listed and recent matches. Add a company (it finds the board for you), turn boards on
  or off, change how often they're polled, remove them.
- **Map:** every US metro colored by the pay index at any salary, renting or buying, with the
  salary that breaks even with staying where you are, and your top jobs.
- **Stories:** your work stories, with a live preview of exactly what the AI reads. After you
  change them, re-score recent jobs in the background.
- **Settings:** your situation (salary, home, housing cost, household, stay or move), what to look
  for, scoring and alerts, notifications and Home Assistant, API keys (write-only: never shown
  again once saved), profile and resumes, password and the Home Assistant map link.

Changes apply at once, without a restart.

## Set it up for you

1. **Settings → Your situation:** salary, where you live, what your home costs a month, household
   and whether you'd move. This is what the pay index compares every job with.
2. **Settings → Profile and resumes:** what you want next and your deal-breakers; upload resumes.
3. **Stories:** write a story for each piece of work you'd tell in an interview. The more specific
   the result, the better the scores.
4. **Settings → What to look for:** field and role words for job titles.
5. **Boards:** add the companies you want to watch.
6. **Settings → API keys:** an [Anthropic](https://console.anthropic.com) key for AI scoring (about
   $0.001 per job), and an [ntfy](https://ntfy.sh) topic for phone pushes. Optional: USAJobs, Adzuna,
   JSearch and TheirStack keys for searches beyond your watch list.
7. **Settings → Connections:** the address your phone uses to reach this machine, so a tap on an
   alert opens the job here.

## Running it 24/7 or on a desktop

Same program, no switch. On a server it simply never stops. On a PC that's off or asleep part of
the time, it notices the gap when it starts or wakes, polls every board at once, looks back over
the gap in the aggregator searches, and sends one summary push plus the best three instead of a
push per job. The rest wait on the jobs page ("Since you were away") and in the digest. A digest
that came due while it was off is sent once. What it can't recover: jobs posted and taken down
while the PC was off, and boards that only list their newest jobs may have moved past some.

- Server: [docs/deploy.md](docs/deploy.md) (Docker, Proxmox LXC).
- Desktop or laptop, starting at login: [docs/desktop-mode.md](docs/desktop-mode.md).

Only one `jobalerts run` can use a database at a time.

## Where things live

| Where | What |
|---|---|
| `data/` | Everything of yours: the database, settings, the encrypted key store and its key (`secret.key`), profile, stories, resumes, watch list, `config.local.yaml`. Back it up; keep it private. |
| `config/` | Shipped defaults and data tables (prices, taxes). Don't edit; override on the Settings page or in `data/config.local.yaml`. |
| `.env` | Optional: keys as environment variables (they win over keys saved on the Settings page). |

Details: [docs/configuration.md](docs/configuration.md) (every setting, the keyword rules, AI
scoring, the pay index, Home Assistant and the map), [docs/security.md](docs/security.md) (what's
protected and how), [docs/architecture.md](docs/architecture.md) (how it's built).

## Commands

```
jobalerts [-c CONFIG] [-v] {run,web,set-password,update,check,backup,poll,discover,test-notify,digest,top,stats,backfill,map,pay}

  run                  the service: polls every board on its interval, with the web UI on port 8765
  web                  the web UI only (no polling or pushes), over the same database
  set-password         set or reset the web UI password
  poll [--dry-run] [--board TEXT] [--score N]
                       one pass over every board (--dry-run stores and sends nothing)
  discover [TARGET ...] [--file FILE] [--add] [--tier TIER] [--name NAME]
                       find companies' job boards by name, careers URL or a list file
  test-notify          send a sample alert to your ntfy topic
  digest [--dry-run]   send the daily digest now
  top [-n N] [--days D] [--push]
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

jobalerts checks its git repository once a day and tells you about a new version, or installs it
itself (Settings → Updates; `jobalerts update`). A Docker install updates from the host with
`tools/update-docker.sh`, daily with the timer in `contrib/`. By hand: `git pull`, then
`docker compose up -d --build` (or `pip install -e .` and restart). Your settings,
keys, watch list, stories, resumes and database are all in `data/`, which updates never touch; the
database is backed up before an upgrade changes it, and a setting a new version can't use is set
aside on its own (the Settings page lists it) instead of stopping anything. Details and the few ways
you could still lose something: [docs/upgrading.md](docs/upgrading.md).

### Upgrading an install from before 2026-11-24

Older versions kept the profile, watch list, stories and resumes in tracked files, and pulling
the new version deletes them from the working tree. Move them into `data/` first:

```sh
git fetch && git show origin/main:tools/migrate_user_data.sh | sh
git pull && docker compose up -d --build
```

The script never overwrites anything in `data/`. On the next start your old `config.yaml` becomes
`data/config.local.yaml` (only the values you had changed) and your old household moves with it.
The Map card in Home Assistant needs the new share link from Settings → Account.

## Limitations

- Companies on iCIMS, Taleo, Eightfold or in-house career sites have no clean public feed; the
  aggregator searches catch some of their postings.
- BambooHR and Rippling lists carry no posting date until a match is fetched in full; Workday
  says "Posted 3 Days Ago" until then.
- The pay index is base pay after taxes against prices: no bonus, equity or benefits. Taxes
  assume you live in the job's city; tax rates abroad are rough (±5 points) and prices abroad are
  country averages.
- An ATS sometimes reposts an old job with a new ID. Jobs posted more than `max_age_days` ago are
  never alerted.

## Development

```sh
pip install -e .[dev] && pytest && ruff check src tests tools
git config core.hooksPath .githooks      # the privacy guard: refuses personal data and credentials in commits
```

Contributions: keep personal data out (it belongs in `data/`, which git ignores). The guard checks
staged changes against a list of your own private terms in `~/.config/jobalerts/private-terms.txt`
(never committed); CI runs the same check for files and credentials.

Adapters live in `src/jobalerts/sources/`, one module per ATS or aggregator, each with a fixture
test in `tests/test_sources.py`. The web UI is FastAPI + Jinja + htmx (no JavaScript build), in
`src/jobalerts/web/`. Work is planned in `PLAN.md`; [CLAUDE.md](CLAUDE.md) has the conventions.
