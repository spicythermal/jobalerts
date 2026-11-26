# Configuration reference

Most of this can be set on the web UI's Settings and Boards pages. This is the full reference for
the files: `config/config.yaml` holds the shipped defaults, and your own changes go in
`data/config.local.yaml` (only what differs, merged on top) or on the Settings page (stored in the
database, encrypted for personal values; the Settings page wins). Keys go in `.env` or on the
Settings page → API keys.

| Where | What goes in it |
|---|---|
| `.env` | Optional secrets: `NTFY_TOPIC`, `ANTHROPIC_API_KEY`, `MQTT_PASSWORD`, aggregator keys (or set them on Settings → API keys). Never committed. |
| `config/config.yaml` | Shipped defaults: criteria, polling tiers, thresholds, digest time, aggregators. Don't edit; override instead. |
| `data/config.local.yaml` | Your overrides of `config.yaml` (only what differs). |
| `data/companies.yaml` | Your watch list (Boards page, or `jobalerts discover --add`). Starts from a 10-company example. |
| `data/stories/` | Your work stories, one project per file (Stories page; format in `src/jobalerts/examples/stories/README.md`). AI scoring judges fit against all of them and names the best 3 for each job. |
| `data/resumes/` | Your resumes (PDF, `.md` or `.txt`; Settings → Profile and resumes). With stories, the AI only uses their opening section to say which version to apply with; without stories, it scores against the full resumes. |
| `data/profile.md` | What the stories and resumes don't say: target roles, what makes a role impactful, deal-breakers (Settings → Profile and resumes). |

**Criteria** (`config.yaml` → `criteria`) are case-insensitive whole-word phrases that also
match plurals ("beam" matches "Beams"); a `*` at either end lets a phrase run into a longer word
("*physicist" matches "Cryophysicist", "research*" matches "Researcher"). A title passes with a
`title_field` word (the work: plasma, ion beam, laser…) or a `title_include` word (the role:
scientist, systems engineer, group lead…). `title_exclude`, `seniority_exclude` and
`description_exclude` always reject a job; `title_exclude_soft` rejects it unless the title has a
field word ("Applied Plasma Data Scientist" stays, "Data Scientist, Marketing" goes). `locations` must match unless the job is remote and `allow_remote: true`; an
empty list (the shipped default) means anywhere, and the AI score ranks by your profile.

**Adding companies:**

```sh
jobalerts discover "Cloudflare" --add                      # probes Greenhouse/Lever/Ashby/SmartRecruiters/Personio/Workable
jobalerts discover https://company.com/careers --add       # finds ATS links on a careers page (any supported ATS)
jobalerts discover "Tailscale" --add --tier priority
jobalerts discover --file my-candidates.txt --add         # a whole list; already-listed boards are skipped
```

A list file takes `Company Name` or `Company Name | https://careers.url` per line. Companies
on Taleo, iCIMS, Eightfold, Oracle or in-house career sites aren't supported and are listed as
not found at the end of the run; the aggregators still catch many of their postings.

For Workday, SmartRecruiters, UltiPro and SuccessFactors, add `search:` to the entry so big
enterprise boards stay small (see the comments in `companies.yaml`). SuccessFactors sites are
read through their RSS feed, which holds the newest 20 postings.

**Phone:** install the ntfy app (Android/iOS) and subscribe to your `NTFY_TOPIC` on
`ntfy.sh`. Pick a long random topic name, since anyone who knows it can read it.
A self-hosted ntfy works too: set `notify.server` and `NTFY_TOKEN`.

**Features switch on with their keys.** AI scoring, the Home Assistant sensor and the four
aggregators are all `enabled: true` in the shipped `config.yaml`, and each one stays idle
until its key is set (Settings → API keys, or `.env`); saving a key starts it without a
restart. To turn something off for good, switch it off on the Settings page or set
`enabled: false` in `data/config.local.yaml`.

**Aggregator budgets.** Adzuna's free key is a 25-call trial; polling needs its £8/month plan
(500 calls a day). TheirStack charges one credit per job returned (free plan: 200 a month), so
each TheirStack poll only asks for jobs found since the last poll, and `limit` caps a poll's cost.
When the credits run out the board reports "out of API credits (402)" and backs off until they refill.

**AI scoring:** write your work stories (Stories page), add your resumes and profile (Settings →
Profile and resumes), and set the Anthropic API key (Settings → API keys). Keyword matches are scored 0–100 by
Claude Haiku 5.5 against the stories and profile (about $0.001 each). A story library sees more
than any one resume: every project, your role in it and its results. Unfinished parts marked
`TODO` and the talk and slide notes are left out of the prompt. Each alert lists **your 3
best-fitting stories** ("Your best stories: …") and which resume version to apply with
("Apply with: …"), and so do the digest, `jobalerts top` and Home Assistant. With no stories,
scoring falls back to the full resumes.
A score of 75 or more is pushed now, 50–74 goes to the evening digest, and anything
lower is only stored. If scoring fails or the daily cap is reached, keyword matches are
still pushed, except when the daily AI budget is used up: then they wait in the digest.

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

**Keywords first, or AI only** (`criteria.mode`, the Filter page). With `keywords` the word lists
decide and the AI second look below catches what they miss. With `ai` the word lists are off except
`title_exclude`, `seniority_exclude`, `description_exclude` and `max_age_days`: every other new job
goes through the same two cheap passes, then the full score. Batching keeps the calls down: titles go
50 to a call and excerpts 8 to a call, so a day of 400 new jobs costs about 8 title calls, ~18 excerpt
calls and ~70 full scores. The Filter page estimates this from your own last week. The first look
runs on `prescreen.model` ("" = `scoring.model`); `prescreen.guidance` adds your own words about what
to turn down or keep, and your field words (`criteria.title_field`) are included as your field. In
AI-only mode the first look runs `prescreen.ai_delay` seconds (120) after new jobs arrive, not hourly.
`scoring.daily_max_calls` is this program's own budget for all Claude calls; when it's spent, waiting
jobs keep until the next UTC day.

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
relative to your current salary where you live now (`pay.reference_salary` at
`pay.reference_location`, set on the Settings page) = 1.00. The same pay somewhere with no state
income tax and similar prices scores above 1; a high-tax, high-rent city scores below it.
Take-home pay is base pay after federal income tax, Social Security and Medicare, state income tax,
state payroll taxes (California SDI, paid-leave programs, WA Cares, Oregon transit) and city or
county income taxes where the job is, for your household (filing status, dependents and pre-tax
401(k) share; the 401(k) money counts as yours, it only lowers income tax).
Set the household on the Settings page (or under `household:`); the 401(k) money only lowers the
taxes levied on income after it, not Social Security, Medicare or wage taxes such as Philadelphia's.
US taxes come from `config/tax_data/`, tables of 2026 taxes by salary for every state and the
localities in `taxes.yaml`, one per household (single or joint, 0–4 children under 17, 0–1 older
dependents), computed with PolicyEngine-US 2.88.9. City taxes PolicyEngine doesn't model
(Ohio, Pennsylvania, Michigan and Kentucky cities, Portland Metro's housing tax) and approximate
rates abroad are in `taxes.yaml` too. Rebuild the tables each January (the digest reminds you):
`pip install "policyengine-us==2.88.9" && python tools/build_tax_data.py` (Python 3.10–3.14; review the numbers
before moving to a newer PolicyEngine). `pay.taxes: false` compares gross
pay instead. US prices come
from the BEA's Regional Price Parities (by metro area, else by state), with housing priced for a
family that moves: `pay.housing_share` (40%) of spending goes to rent plus utilities at HUD's
4-bedroom Fair Market Rent for the metro, and the rest to the RPP's goods and other services. The
plain all-items RPP weighs housing at about 17% at what all renters pay, which understates how much
dearer family housing is in expensive metros. States without an FMR use the RPP's rents;
`housing_share: 0` goes back to the plain RPP.

**Renting or buying:** US jobs get two indexes, renting and buying (`pay.buy`). The buying one
prices the housing share at the monthly cost of owning the area's typical 4-bedroom home instead
of renting one: Zillow's 4-bedroom Home Value Index for the metro (else the state; data by
Zillow), 20% down, Freddie Mac's latest 30-year rate on the loan, property tax by state, upkeep
(insurance and maintenance, 0.75% a year) and utilities. Paying down principal counts as saving,
so it's left out; so are the mortgage-interest deduction and what the down payment would earn.
Two settings make it a move-vs-stay comparison:

- `pay.current_home`: what staying in your own home costs now (mortgage payment, less the
  principal it pays down, plus upkeep and utilities: e.g. $2,500 − $600 + $300 + utilities, at
  your locked-in rate). Both indexes compare a move with that, and jobs in your home metro count
  as staying (1.0 either way). Buying elsewhere means today's rate, so a low locked rate counts
  against moving. `payment: 0` compares with market rent and price instead.
- `pay.city_weight` (0.5): a metro's typical home and rent can be far from the job's city's (a
  rich suburb's 4-bedroom homes can cost twice the metro's). Zillow's city figures (4-bedroom
  values; typical rents against the metro's) move the metro's numbers halfway toward the job's
  city: living near the job rather than in it. 0 = anywhere in the metro, 1 = in the city.

In an expensive coastal city the buying index can fall far below the renting one (a multi-million
4-bedroom at today's rates), while cheaper metros stay close either way. A job passes
`pay.min_index` if *either* index clears it, and alerts show both ("pay index 1.22 rent · 0.48 buy
… own $3.00M 4-bed at 7.18%: $20K/mo, $600K down"). Selling and moving costs (one-time) aren't in
either index. Prices abroad come from the
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
2. On Settings → Connections set the MQTT host (the Home Assistant box) and user `jobalerts`,
   and the password under Settings → API keys (or `MQTT_HOST`, `MQTT_USERNAME`, `MQTT_PASSWORD`
   in `.env`, then restart).
3. Run `jobalerts top --push` to check. A "Job alerts" device with `sensor.job_alerts`
   appears (MQTT discovery). Its state is the number of matches, and its `jobs` attribute
   holds the top matches with their AI score (`score`), step-up score (`step_up`, `step_up_note`)
   and pay indexes (renting `pay_index`, range `pay_low`–`pay_high`; buying `buy_index`,
   `buy_low`–`buy_high`), the ids of its 3 best-fitting work stories (`story_ids`), and its `step_ups` attribute the biggest step ups in the same form. It refreshes every 5 minutes and right after new matches. The messages
   are retained, so the sensor survives Home Assistant restarts. It turns "unavailable" if
   jobalerts stops updating it for 15 minutes.
4. Add a Markdown card to a dashboard (Edit dashboard → Add card → Manual). Under each job it shows
   both pay indexes with their ranges, renting and buying (see "Renting or buying" below), e.g.
   `Rent 1.22 (1.10–1.35) · Buy 0.48 (0.40–0.55)`. A job is listed when either range reaches
   `pay.min_index`. A single salary shows no range.
   Paste the whole block in the card's code editor (*Show code editor*). To use the visual
   editor instead, put the title in *Title* and everything under `content: |-` (without the
   two-space indent) in *Content*.

```yaml
type: markdown
title: Top job matches
content: |-
  {%- macro scale(name, i, lo, hi) -%}
  {{ name }} {{ '%.2f'|format(i) }}{% if lo is not none and hi is not none and lo != hi %} ({{ '%.2f'|format(lo) }}–{{ '%.2f'|format(hi) }}){% endif %}
  {%- endmacro %}
  {%- for j in state_attr('sensor.job_alerts', 'jobs') or [] %}
  **[{{ j.title }}]({{ j.url }})**
  {{ j.company }} · {{ j.location }}{% if j.salary %} · {{ j.salary }}{% endif %} · {{ j.age }}{% if j.score is not none %} · fit {{ j.score }}{% endif %}
  {%- if j.pay_index is not none %}
  {{ scale('Rent', j.pay_index, j.pay_low, j.pay_high) }}{% if j.buy_index is not none %} · {{ scale('Buy', j.buy_index, j.buy_low, j.buy_high) }}{% endif %}
  {%- endif %}
  {%- if j.story_ids %}
  <small>Stories: {{ j.story_ids | join(' · ') }}</small>
  {%- endif %}
  {%- if j.best_resume %}
  <small>Apply with {{ j.best_resume }}</small>
  {%- endif %}

  {% else %}
  No matches in the last {{ state_attr('sensor.job_alerts', 'days') or 7 }} days.
  {% endfor %}
```

A second card for the biggest step ups uses the `step_ups` attribute:

```yaml
type: markdown
title: Biggest step ups
content: |-
  {%- macro scale(name, i, lo, hi) -%}
  {{ name }} {{ '%.2f'|format(i) }}{% if lo is not none and hi is not none and lo != hi %} ({{ '%.2f'|format(lo) }}–{{ '%.2f'|format(hi) }}){% endif %}
  {%- endmacro %}
  {%- for j in state_attr('sensor.job_alerts', 'step_ups') or [] %}
  **[{{ j.title }}]({{ j.url }})** · step up {{ j.step_up }} · fit {{ j.score }}
  {{ j.company }} · {{ j.location }}{% if j.salary %} · {{ j.salary }}{% endif %}
  {%- if j.pay_index is not none %}
  {{ scale('Rent', j.pay_index, j.pay_low, j.pay_high) }}{% if j.buy_index is not none %} · {{ scale('Buy', j.buy_index, j.buy_low, j.buy_high) }}{% endif %}
  {%- endif %}
  {%- if j.step_up_note %}
  <small>{{ j.step_up_note }}</small>
  {%- endif %}
  {%- if j.story_ids %}
  <small>Stories: {{ j.story_ids | join(' · ') }}</small>
  {%- endif %}

  {% else %}
  No step ups scored yet.
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

### Cost-of-living map

The **Map** page of the web UI (port 8765, `web:` in `config.yaml`, published in
`docker-compose.yml`). Every US metro is a dot colored by how well off you'd be there compared with
`pay.reference_salary` staying in your own home: blue is better, gray about the same, red worse.

- **Pay index** at any salary (slider, $100K–$400K), **renting** or **buying**.
- **Break-even salary**: what a metro must pay to match your current pay at home. Renting or buying.
- **Jobs**: your top matches (diamonds) and biggest step ups (rings) at their metro, with fit, pay
  index and top stories; jobs abroad or in an unknown place are listed under *Table & details*.
- **Table & details**: every metro's numbers, sortable and filterable, plus the sources.

The numbers are the same ones `jobalerts pay` shows. A dot is metro-wide; a job's own index also
blends in its city, so a job in Tacoma can differ a little from the Seattle dot. Selling and moving
costs aren't in either. The page refreshes with the dashboard (`homeassistant.interval`, and right
after new matches) and after the price data is rebuilt.

**Home Assistant:** the web UI needs a login, so the card uses the read-only share link from
Settings → Account (`/map/embed?token=…`; "Make a new link" there retires the old one). It opens the
map and nothing else. Add a Webpage card (Edit dashboard → Add card → Webpage, or Manual):

```yaml
type: iframe
url: http://<jobalerts-host>:8765/map/embed?token=<your token>&view=breakeven&housing=buy
aspect_ratio: 75%
title: Where the move pays
```

The URL can preset the view, so several cards can show different maps:
`view=index|breakeven`, `housing=rent|buy`, `salary=250000`, `jobs=0`, `theme=dark|light` (the page
otherwise follows the browser), `panel=1` (opens the table). Changes you make in the page are
remembered per browser.

The browser opening Home Assistant loads the map straight from jobalerts, so it must reach
`<jobalerts-host>:8765`. If you open Home Assistant over **https** (Nabu Casa, or a reverse proxy),
the browser blocks a plain-http page inside it: put jobalerts behind the same proxy with TLS (for
Caddy, `jobs.home.lan { reverse_proxy <jobalerts-host>:8765 }`), set `web.secure_cookies: true`,
and use that https URL in the card. `web.frame_origins` can limit which sites may embed the map.
The page loads Leaflet and the state outlines from public CDNs, so it needs internet access too.

`jobalerts map --out FILE` writes the same page to a file you can open anywhere.
