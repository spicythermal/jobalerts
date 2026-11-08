# jobalerts

Watches employers' own job boards and pushes newly posted, matching jobs to your phone
within minutes. LinkedIn and Indeed copy listings from those boards hours or days later.

Most companies publish openings first in their applicant tracking system (ATS): Greenhouse,
Lever, Ashby, SmartRecruiters, Workday, UKG/UltiPro, Personio, Workable, BambooHR, Rippling,
Breezy or SuccessFactors. Each ATS serves the company's careers page from a public JSON (or
XML/RSS) feed, and jobalerts reads those feeds directly:

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
- **Broad net:** USAJobs, Adzuna, JSearch and TheirStack searches catch companies you don't
  watch. Each runs once its API key is in `.env` and is skipped until then. Their matches also feed "hiring for your search, not on your watch list"
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
| `.env` | Secrets: `NTFY_TOPIC`, `ANTHROPIC_API_KEY`, `MQTT_PASSWORD`, aggregator keys. Never committed. |
| `config/config.yaml` | Search criteria, polling tiers, scoring thresholds, digest time, aggregators. |
| `config/companies.yaml` | The watch list, generated from `config/candidates.txt`. |
| `config/candidates.txt` | Companies to resolve into the watch list, one per line (see below). |
| `resume_reference/` | Your resumes (PDF, `.md` or `.txt`). AI scoring compares every posting against all of them. Committed (private repo) and mounted read-only into the container. |
| `config/profile.md` | What the resumes don't say: target roles, location, salary floor, deal-breakers. |

**Criteria** (`config.yaml` → `criteria`) are case-insensitive whole-word phrases that also
match plurals ("beam" matches "Beams"); a `*` at either end lets a phrase run into a longer word
("*physicist" matches "Cryophysicist", "research*" matches "Researcher"). A title passes with a
`title_field` word (the work: plasma, ion beam, laser…) or a `title_include` word (the role:
scientist, systems engineer, group lead…). `title_exclude`, `seniority_exclude` and
`description_exclude` always reject a job; `title_exclude_soft` rejects it unless the title has a
field word ("Applied Plasma Data Scientist" stays, "Data Scientist, Marketing" goes). `locations` must match unless the job is remote and `allow_remote: true`; an
empty list (the shipped default) means anywhere, and the AI score ranks by `profile.md`.

**Adding companies:**

```sh
jobalerts discover "Cloudflare" --add                      # probes Greenhouse/Lever/Ashby/SmartRecruiters/Personio/Workable
jobalerts discover https://company.com/careers --add       # finds ATS links on a careers page (any supported ATS)
jobalerts discover "Tailscale" --add --tier priority
jobalerts discover --file config/candidates.txt --add      # a whole list; already-listed boards are skipped
```

`candidates.txt` takes `Company Name` or `Company Name | https://careers.url` per line. Companies
on Taleo, iCIMS, Eightfold, Oracle or in-house career sites aren't supported and are listed as
not found at the end of the run; the aggregators still catch many of their postings.

For Workday, SmartRecruiters, UltiPro and SuccessFactors, add `search:` to the entry so big
enterprise boards stay small (see the comments in `companies.yaml`). SuccessFactors sites are
read through their RSS feed, which holds the newest 20 postings.

**Phone:** install the ntfy app (Android/iOS) and subscribe to your `NTFY_TOPIC` on
`ntfy.sh`. Pick a long random topic name, since anyone who knows it can read it.
A self-hosted ntfy works too: set `notify.server` and `NTFY_TOKEN`.

**Features switch on from `.env`.** AI scoring, the Home Assistant sensor and the four
aggregators are all `enabled: true` in the shipped `config.yaml`, and each one stays idle
until its keys are in `.env`. So the server needs no edits to tracked files, and `git pull`
never overwrites a setting. To turn something off for good, set `enabled: false` on your PC,
commit and push.

**Aggregator budgets.** Adzuna's free key is a 25-call trial; polling needs its £8/month plan
(500 calls a day). TheirStack charges one credit per job returned (free plan: 200 a month), so
each TheirStack poll only asks for jobs found since the last poll, and `limit` caps a poll's cost.
When the credits run out the board reports "out of API credits (402)" and backs off until they refill.

**AI scoring:** put your resumes in `resume_reference/`, fill in `config/profile.md`, and set
`ANTHROPIC_API_KEY`. Keyword matches are scored 0–100 by
Claude Haiku 5.5 against the resumes and profile (about $0.001 each with two resumes). Each
alert says which resume version to apply with ("Apply with: …"), and so does the digest.
A score of 75 or more is pushed now, 50–74 goes to the evening digest, and anything
lower is only stored. If scoring fails or the daily cap is reached, keyword matches are
still pushed.

The same call also gives a separate **step-up score** (0–100): how big a career step up the role
is (title level, scope, team size, budget, authority) *and* how credibly you could win it. It is
not the fit score: a strong fit at your level scores high on fit but low on step-up, while a
director role you could stretch to scores high on step-up even if fit is middling. A step-up of
`scoring.step_up_threshold` (75) or more is pushed whatever the fit score, with a 🚀 tag and a
"Step up 82/100: …" line; from 60 the note is shown in alerts and the digest. `jobalerts top` and
Home Assistant list the biggest step ups separately from the best fits. Preview scores before
turning it on:

```sh
jobalerts poll --dry-run --score 5
```

**AI second look at rejected titles** (`prescreen` in `config.yaml`, on whenever scoring is):
keyword lists miss good jobs with unexpected titles ("Manager, Generator Operations" at a fusion
company). A job the keywords turn down, but no `title_exclude` / `seniority_exclude` phrase did, waits
in a queue. Every hour two cheap Claude calls go through the queue: clear misfits by title are dropped,
then clear misfits by title, company and the start of the description; the rest get the normal
0–100 score and are pushed or digested like any match. If Claude can't be reached they keep waiting.
Tested on a week of real postings: it kept both rejected jobs the full score rated 75+, and two
thirds of those rated 50+; about 17% go on to full scoring, roughly $1 a month. `jobalerts poll`
runs it at the end too.

**Cost-of-living pay index:** every match gets a pay index: take-home pay divided by local prices,
relative to `pay.reference_salary` at `pay.reference_location` ($155K in Portland = 1.00; $155K in
Austin = 1.18, from no state income tax and slightly lower prices; $155K in New York City = 0.93).
Take-home pay is base pay after federal income tax, Social Security and Medicare, state income tax,
state payroll taxes (California SDI, paid-leave programs, WA Cares, Oregon transit) and city or
county income taxes where the job is. The household is set in `config/taxes.yaml`: married filing
jointly, one earner, dependents (ages 4, 8 and 18), 6% pre-tax 401(k) (that money counts as
yours; it only lowers income tax).
US taxes come from `config/tax_data.json`, a table of 2026 taxes by salary for every state and the
localities in `taxes.yaml`, computed with PolicyEngine-US. City taxes PolicyEngine doesn't model
(Ohio, Pennsylvania, Michigan and Kentucky cities, Portland Metro's housing tax) and approximate
rates abroad are in `taxes.yaml` too. Rebuild the table each January (the digest reminds you):
`pip install policyengine-us && python tools/build_tax_data.py`. `pay.taxes: false` compares gross
pay instead. US prices come
from the BEA's Regional Price Parities (by metro area, else by state), with housing priced for a
family that moves: `pay.housing_share` (40%) of spending goes to rent plus utilities at HUD's
4-bedroom Fair Market Rent for the metro, and the rest to the RPP's goods and other services. The
plain all-items RPP weighs housing at about 17% at what all renters pay, so it put San Jose only 5%
above Portland; with family housing it's about 27% above. States without an FMR use the RPP's
rents; `housing_share: 0` goes back to the plain RPP.

**Renting or buying:** US jobs get two indexes, renting and buying (`pay.buy`). The buying one
prices the housing share at the monthly cost of owning the area's typical 4-bedroom home instead
of renting one: Zillow's 4-bedroom Home Value Index for the metro (else the state; data by
Zillow), 20% down, Freddie Mac's latest 30-year rate on the loan, property tax by state, upkeep
(insurance and maintenance, 0.75% a year) and utilities. Paying down principal counts as saving,
so it's left out; so are the mortgage-interest deduction and what the down payment would earn.
Two settings make it a move-vs-stay comparison:

- `pay.current_home`: what staying in your own home costs now (mortgage payment, less the
  principal it pays down, plus upkeep and utilities: e.g. $2,500 − $600 + $300 + utilities ≈ $2,550 a
  month at your locked-in rate). Both indexes compare a move with that, and jobs in your home
  metro count as staying (1.0 either way). Buying elsewhere means today's rate, so the low
  locked rate counts against moving. `payment: 0` compares with market rent and price instead.
- `pay.city_weight` (0.5): a metro's typical home and rent can be far from the job's city's
  (Palo Alto's 4-bedroom homes cost 2.2x the San Jose metro's). Zillow's city figures (4-bedroom
  values; typical rents against the metro's) move the metro's numbers halfway toward the job's
  city: living near the job rather than in it. 0 = anywhere in the metro, 1 = in the city.

With both, $250K in Palo Alto is 1.22 renting but 0.48 buying (a $3.0M blended 4-bedroom at
7.18%: about $20K a month and $600K down), while Austin and Albuquerque stay above 1.1 either
way. A job passes `pay.min_index` if *either* index clears it, and alerts show both ("pay index
1.22 rent · 0.48 buy … own $3.00M 4-bed at 7.18%: $20K/mo, $600K down"). Selling and moving
costs (one-time) aren't in either index. Prices abroad come from the
World Bank's purchasing-power parity divided by the exchange rate (country-wide). Remote jobs use
your home prices. The pay comes from the posting's salary field, or else from a salary range stated
in the description. When even the top of that range is under `pay.min_index` (1.05) renting and buying, the match is
stored as `low_pay` *before* AI scoring: no Claude call, no alert. The digest lists those jobs with
their pay note so you can catch a misread. A posting that states no pay is still scored; the AI
reads pay from the text, and the same check applies afterwards. The index shows in alerts, the
digest, `top` and Home Assistant. `jobalerts pay "$150K-180K" "Austin, TX"` shows how one case is
worked out. The price data refreshes itself: once a day the service checks it, and when it's older
than `pay.refresh_days` it downloads new tables into `data/col_data.json`. They replace the old
data only if they pass sanity checks; if not, the old data stays and the digest says why.
`jobalerts pay --update` refreshes now. The seed copy is `config/col_data.json`.

**Top matches / Home Assistant:** `jobalerts top` lists the 10 best matches posted in the last 7 days,
ranked by AI score and then newest, then the 10 biggest step ups (by step-up score). Jobs that
were already open when watching started count too. To show the list on a Home Assistant dashboard, jobalerts publishes it to Home
Assistant's MQTT broker. It never gets a Home Assistant token, only an MQTT login:

1. Give it its own broker login. With the Mosquitto add-on: Settings → Add-ons → Mosquitto
   broker → Configuration → *Logins*, add `username: jobalerts` and a password, and restart
   the add-on. (The MQTT integration must already be set up in Home Assistant.)
2. Put `MQTT_HOST=<home-assistant-ip>`, `MQTT_USERNAME=jobalerts` and `MQTT_PASSWORD=...` in
   `.env`, and restart jobalerts.
3. Run `jobalerts top --push` to check. A "Job alerts" device with `sensor.job_alerts`
   appears (MQTT discovery). Its state is the number of matches, and its `jobs` attribute
   holds the top matches with their AI score (`score`), step-up score (`step_up`, `step_up_note`)
   and pay indexes (renting `pay_index`, range `pay_low`–`pay_high`; buying `buy_index`,
   `buy_low`–`buy_high`), and its `step_ups` attribute the biggest step ups in the same form. It refreshes every 5 minutes and right after new matches. The messages
   are retained, so the sensor survives Home Assistant restarts. It turns "unavailable" if
   jobalerts stops updating it for 15 minutes.
4. Add a Markdown card to a dashboard (Edit dashboard → Add card → Manual):

```yaml
type: markdown
title: Top job matches
content: >-
  {% for j in state_attr('sensor.job_alerts', 'jobs') or [] %}
  **[{{ j.title }}]({{ j.url }})**<br>
  {{ j.company }} · {{ j.location }}{% if j.salary %} · {{ j.salary }}{% endif %} · {{ j.age }}
  {%- if j.score is not none %} · fit {{ j.score }}{% endif %}
  {%- if j.pay_index is not none %} · pay {{ j.pay_index }} rent{% if j.buy_index is not none %} / {{ j.buy_index }} buy{% endif %}{% endif %}<br>
  {% if j.best_resume %}<small>Apply with {{ j.best_resume }}</small>{% endif %}

  {% else %}
  No matches in the last {{ state_attr('sensor.job_alerts', 'days') or 7 }} days.
  {% endfor %}
```

A second card for the biggest step ups uses the `step_ups` attribute:

```yaml
type: markdown
title: Biggest step ups
content: >-
  {% for j in state_attr('sensor.job_alerts', 'step_ups') or [] %}
  **[{{ j.title }}]({{ j.url }})** · step up {{ j.step_up }} · fit {{ j.score }}<br>
  {{ j.company }} · {{ j.location }}{% if j.pay_index is not none %} · pay {{ j.pay_index }} rent{% if j.buy_index is not none %} / {{ j.buy_index }} buy{% endif %}{% endif %}<br>
  <small>{{ j.step_up_note }}</small>

  {% endfor %}
```

**AI cost:** while AI scoring is on, the same device also gets
`sensor.job_alerts_ai_cost_this_month` (USD): an estimate of this month's scoring spend at
list prices. Its attributes hold `today`, `last_7_days`, `calls_month` and `calls_7_days`.
It counts only jobalerts' own calls; the Console's Billing page is the real bill (Anthropic
has no API for the prepaid balance). `jobalerts stats` prints the same numbers. A card:

```yaml
type: entity
entity: sensor.job_alerts_ai_cost_this_month
name: Claude API cost this month
```

**Optional: limit what that login can do.** By default any broker login can publish to any
topic. To let `jobalerts` write only its own sensor, set the Mosquitto add-on's
`customize` option to `active: true, folder: mosquitto`, then create two files in `/share/mosquitto/`:

```
# acl.conf
acl_file /share/mosquitto/accesscontrollist
```

```
# accesscontrollist — once this file exists, every login must be listed
user homeassistant
topic readwrite #

user addons
topic readwrite #

user jobalerts
topic write jobalerts/#
topic write homeassistant/sensor/jobalerts/#
```

Add a `user` block for each other login you use (Zigbee2MQTT and so on), and restart the
add-on. Mosquitto drops a publish the list doesn't allow without telling jobalerts, so if the
sensor doesn't appear, check this file first.

## Commands

```
jobalerts [-c CONFIG] [-v] {run,poll,discover,test-notify,digest,top,stats,backfill,pay}

  run                  the service: poll every board on its tier interval, forever
  poll [--once] [--dry-run] [--board TEXT] [--score N]
                       one pass over every board (--dry-run stores and sends nothing)
  discover [TARGET ...] [--file FILE] [--add] [--tier TIER] [--name NAME]
                       find companies' ATS boards by name, careers URL, or a list file
  test-notify          send a sample alert to your ntfy topic
  digest [--dry-run]   send the daily digest now
  top [-n N] [--days D] [--push]
                       best recent matches; --push also updates the Home Assistant sensor
  stats                board health, request rates, detection delay, AI spend
  backfill [--dry-run] [--push] [--days N]
                       re-check the pay index of processed matches, then AI-score the
                       matches that were already open at the first poll
  pay [SALARY LOCATION] [--remote] [--update]
                       explain the cost-of-living pay index; --update downloads fresh price data
```

`-c` defaults to `config/config.yaml` (or `$JOBALERTS_CONFIG`). `$JOBALERTS_DB` overrides the
database path.

## Deploy on the homelab (Docker)

### 1. Proxmox LXC only: allow nesting

Docker won't start inside an LXC container without it. On the Proxmox host (or tick
both boxes in the GUI under the container's Options → Features):

```sh
pct set <CTID> --features nesting=1,keyctl=1   # keyctl is needed for unprivileged containers
pct reboot <CTID>
```

A full VM doesn't need this.

### 2. Install Docker and the Compose plugin (Debian / Ubuntu)

Commands are written for root; prefix them with `sudo` otherwise. Compose comes from the
`docker-compose-plugin` package and runs as `docker compose` (with a space). The old
standalone `docker-compose` is deprecated.

```sh
apt-get update && apt-get install -y ca-certificates curl git
install -m 0755 -d /etc/apt/keyrings
. /etc/os-release   # sets $ID (debian or ubuntu) and $VERSION_CODENAME
curl -fsSL https://download.docker.com/linux/$ID/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/$ID $VERSION_CODENAME stable" \
  > /etc/apt/sources.list.d/docker.list
apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

docker compose version           # check
docker run --rm hello-world
```

For a non-root user, `usermod -aG docker <user>` lets them run `docker` without sudo
(log out and back in afterward).

### 3. Run jobalerts

Clone the repository on the server. The
resumes in `resume_reference/` come with it and are mounted read-only.

```sh
git clone <repository URL> jobalerts && cd jobalerts
cp .env.example .env && nano .env        # secrets
mkdir -p data && chown 1000:1000 data config   # the container runs as uid 1000
docker compose up -d --build
docker compose logs -f                   # first polls record silent baselines
docker compose exec jobalerts jobalerts stats
docker compose exec jobalerts jobalerts discover "Some Company" --add && docker compose restart
```

Keys and logins go in `.env` on the server. Change everything else (`config/`, resumes) on
your PC, then commit and push. The server only ever runs `git pull`, so pulls never hit
local edits. Restart the container after any config change.

### Updating

```sh
cd jobalerts && git pull
docker compose up -d --build             # rebuilds and recreates the container if the code changed
docker compose restart                   # needed when only config/ or resume_reference/ changed
```

### After the first deploy: score what was already open

The first poll of each board records its open jobs silently, so none of them are scored.
`backfill` sends the ones that pass your filters and were posted within
`criteria.max_age_days` through Claude, the same way a new job would go. Each job is
scored only once, and any job that can't be scored (wrong key, API down) is left to retry
on the next run.

Before that, `backfill` re-checks the pay index of matches already processed in that window
(pushed, in the digest, or held back for pay). It re-fetches each description. When neither the
salary field nor the description states pay, it AI-scores the job once more so the AI can read
the pay (~$0.001 each; `--dry-run` skips this). The new score replaces the old one. Run it after upgrading to the pay index, or after changing `pay.min_index`. Jobs under
the threshold become `low_pay` and drop out of `top` and Home Assistant. A `low_pay` job that now
passes goes back to the digest, or to the baseline to be scored if it never was.

```sh
docker compose exec jobalerts jobalerts backfill --dry-run   # show the scores, save nothing
docker compose exec jobalerts jobalerts backfill             # save: 50+ go in the digest, `top` and HA show scores
docker compose exec jobalerts jobalerts backfill --push      # same, but 75+ are pushed to your phone now
```

## Living with it

- **Daily digest** (default 18:00) lists matches that weren't pushed, companies hiring for
  your search that you don't watch, and 24 h health (jobs seen, pushes, request count,
  rate limits, failing boards). It's sent even on quiet days, so silence never means broken.
- **Health pushes:** a board failing 5 times in a row (bad slug, site change) sends one
  alert, and another when it recovers. A source missing its API key is disabled with an alert.
- **`jobalerts stats`** shows detection delay (first seen minus published) and per-host
  request and 304 counts. Use it to tune tiers.

## Limitations

- Companies on iCIMS, Taleo, Eightfold (e.g. Lam Research, Infineon) or in-house sites have no
  clean public feed. Use the aggregators or watch those companies manually.
- BambooHR and Rippling lists carry no posting date; it is filled in when a keyword match is
  enriched, and `first_seen_at` stands in until then.
- Workday reports "Posted 3 Days Ago", not an exact time, until a job is enriched.
- The pay index covers base pay after taxes against prices: no bonus, equity or benefits. Taxes
  assume you live in the job's city. Tax rates abroad are rough (±5 points) and prices abroad are
  country averages, so London, Zurich or Munich look cheaper than they are. Map a location by hand
  with `pay.locations`.
- An ATS sometimes reposts an old job with a new ID. Jobs published more than
  `max_age_days` ago are never alerted.

## Development

```sh
pip install -e .[dev] && pytest
```

Adapters live in `src/jobalerts/sources/`, one module per ATS or aggregator, each with a
fixture test in `tests/test_sources.py`.
