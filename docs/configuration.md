# Configuration reference

Most settings are on the web UI's Settings, Filter and Boards pages. Settings are read in this order,
each overriding the one before:

1. `config/config.yaml`: shipped defaults. Don't edit it.
2. `data/config.local.yaml`: your overrides (only what differs).
3. The Settings page: stored in the database; personal values encrypted.

With several users: Your situation, the Filter page, Scoring and alerts, profile, stories, resumes and
the ntfy topic and token are each user's own. Connections (including the ntfy server), Updates, Users,
the watch list and the other keys are the install's; admins change them. `data/config.local.yaml` and `.env` are the first
admin's: other users start from `config/config.yaml` for their own sections.

| File | Contents |
|---|---|
| `.env` | Optional keys: `NTFY_TOPIC`, `ANTHROPIC_API_KEY`, `MQTT_PASSWORD`, aggregator keys. Or set them on Settings → API keys. |
| `data/companies.yaml` | Watch list (Boards page, or `jobalerts discover --add`). |
| `data/stories/` | Work stories, one project per file. Format: `src/jobalerts/examples/stories/README.md`. |
| `data/resumes/` | Resumes: PDF, `.md` or `.txt`. |
| `data/profile.md` | Target roles, priorities, deal-breakers. |
| `data/users/<id>/` | Another user's `profile.md`, `stories/` and `resumes/` (the first admin's are in `data/`). |

## Word lists (`criteria`)

Phrases match whole words, any case, plurals included ("beam" matches "Beams"). A `*` at either end
matches a longer word ("*physicist" matches "Cryophysicist").

| Setting | Effect |
|---|---|
| `title_field` | Field words. A title with one passes, and `title_exclude_soft` doesn't apply. |
| `title_include` | Role words. A title with one passes. |
| `title_exclude`, `seniority_exclude` | A title with one is dropped. |
| `title_exclude_soft` | A title with one is dropped unless it has a field word. |
| `description_exclude` | A description with one is dropped. |
| `locations` | The location must contain one, unless the job is remote and `allow_remote` is true. Empty: anywhere. |
| `max_age_days` | Older jobs are stored but not alerted (7). |
| `mode` | `keywords` or `ai`; see below. |

## How jobs reach the AI score

**`mode: keywords`:** the word lists decide. If `prescreen.enabled`, jobs the lists turn down (other
than by `title_exclude` or `seniority_exclude`) wait in a queue. Every `prescreen.interval` (1 hour)
the AI checks the queue in two passes: titles, 50 per call; then title, company and the first 3,000
characters of the description, 8 per call. Jobs that pass both get the full score. On a week of test
postings, about 17% of queued jobs reached the full score.

**`mode: ai`:** only `title_exclude`, `seniority_exclude`, `description_exclude` and `max_age_days`
apply. Every other new job goes through the same two passes, starting `prescreen.ai_delay` seconds
(120) after it arrives. Requires `prescreen.enabled` and an Anthropic key; without them the word
lists are used. Example: 400 new jobs a day take about 8 title calls, 18 excerpt calls and 70 full
scores. The Filter page estimates this from your last week.

| Setting | Effect |
|---|---|
| `prescreen.model` | Model for the two passes. Empty: `scoring.model`. |
| `prescreen.guidance` | Your instructions to the passes, e.g. what to turn down. Your field words and profile are included automatically. |
| `scoring.daily_max_calls` | Maximum Claude calls per UTC day, all stages. When reached, queued jobs wait for the next day. |

## AI scoring

Needs an Anthropic key, stories or resumes, and a profile. Each job that reaches scoring gets one
call to `scoring.model` (Claude Haiku 5.5, about $0.001). It returns:

- a fit score, 0–100;
- a step-up score, 0–100: how much of a promotion the role is and how likely you are to get it;
- red flags, the 3 best-fitting stories and the resume version to apply with.

With stories, the AI judges fit from them and reads only the first 1,500 characters of each resume.
Without stories, it scores against the full resumes. Story sections marked `TODO` and talk notes are
left out.

| Result | Action |
|---|---|
| Fit ≥ `push_threshold` (75) or step-up ≥ `step_up_threshold` (75) | Push now |
| Fit ≥ `digest_threshold` (50) | Evening digest |
| Lower | Stored only |
| Scoring failed | Push now |
| Daily call limit reached | Evening digest |

`step_up_threshold: 0` turns off step-up pushes. Preview scores without sending anything:
`jobalerts poll --dry-run --score 5`.

## Adding companies

```sh
jobalerts discover "Cloudflare" --add                      # probes Greenhouse/Lever/Ashby/SmartRecruiters/Personio/Workable
jobalerts discover https://company.com/careers --add       # finds ATS links on a careers page (any supported ATS)
jobalerts discover "Tailscale" --add --tier priority
jobalerts discover --file my-candidates.txt --add         # one per line: Name, or Name | careers URL
```

Taleo, iCIMS, Eightfold, Oracle and in-house career sites aren't supported; they're listed as not
found. For Workday, SmartRecruiters, UltiPro and SuccessFactors, add `search:` to the entry to limit
results (see the comments in `companies.yaml`). SuccessFactors feeds hold the newest 20 jobs.

## Keys and aggregators

Each feature stays off until its key is set (Settings → API keys or `.env`): AI scoring, Home
Assistant, and the four aggregators. To turn one off with its key set, use the Settings page or
`enabled: false` in `data/config.local.yaml`.

- **Adzuna:** the free key allows 25 calls; polling needs the paid plan (500 calls a day).
- **TheirStack:** one credit per job returned (free plan: 200 a month). Each poll asks only for jobs
  since the last poll; `limit` caps one poll. Out of credits: the board shows "402" and backs off.

## Phone pushes

Install the ntfy app and subscribe to your `NTFY_TOPIC` on `ntfy.sh`. Anyone who knows the topic
name can read it; use a long random name. Self-hosted: set the ntfy server on Settings → Connections
(`notify.server`, one for all users) and each user's token under API keys (`NTFY_TOKEN`).

## Pay index (`pay`)

Pay index = take-home pay ÷ local prices, relative to `pay.reference_salary` at
`pay.reference_location` (= 1.00).

**Take-home pay:** base pay minus federal income tax, Social Security, Medicare, state income tax,
state payroll taxes (California SDI, paid leave, WA Cares, Oregon transit) and local income taxes at
the job's location, for your household (`household`: filing status, children under 17, other
dependents, pre-tax 401(k) share). The 401(k) share reduces income tax only and counts as yours.

- Tax tables: `config/tax_data/`, 2026 taxes by salary for each state and the localities in
  `taxes.yaml`, one table per household type, from PolicyEngine-US 2.88.9. Local taxes PolicyEngine
  lacks, and approximate rates abroad, are in `taxes.yaml`.
- Rebuild each January (the digest reminds you):
  `pip install "policyengine-us==2.88.9" && python tools/build_tax_data.py` (Python 3.10–3.14).
- `pay.taxes: false` uses gross pay.

**Local prices (US):** BEA Regional Price Parities by metro (else state). `pay.housing_share` (40%) of
spending is priced at HUD's 4-bedroom Fair Market Rent plus utilities; the rest at the RPP for goods
and services. `housing_share: 0` uses the plain RPP.

**Abroad:** World Bank purchasing-power parity ÷ exchange rate, country-wide.

**Remote jobs:** priced at your home location.

**Renting and buying:** US jobs get two indexes. The buying index prices housing as owning the
area's typical 4-bedroom home: Zillow's 4-bedroom value (metro, else state), 20% down, Freddie Mac's
latest 30-year rate, property tax by state, upkeep (0.75% a year) and utilities. Principal payments,
the mortgage-interest deduction, the return on the down payment, and selling and moving costs are
not included.

- `pay.current_home`: your current monthly cost of staying (mortgage payment − principal + upkeep +
  utilities, at your rate). Both indexes compare with it; jobs in your home metro are 1.00.
  `payment: 0` compares with market rent and prices.
- `pay.city_weight` (0.5): moves metro housing costs toward the job city's Zillow figures. 0 = metro,
  1 = city.

**Pay gate:** pay comes from the posting's salary field, else a range in the description. If the top
of the range is below `pay.min_index` (1.05) for both renting and buying, the job is stored as
`low_pay` without an AI call or alert; the digest lists these. With no stated pay, the job is scored
and the AI's reading of the pay is checked afterwards. `pay.move_min_index` (1.15) applies instead to
jobs that need a move when Settings → Stay or move is "open".

**Price data:** checked daily; replaced when older than `pay.refresh_days` and the new data passes
sanity checks (else the digest says why). Stored in `data/col_data.json`; the shipped copy is
`config/col_data.json`. `jobalerts pay --update` refreshes now. `jobalerts pay "$150K-180K" "Austin, TX"`
shows one calculation.

## Home Assistant

jobalerts publishes to Home Assistant's MQTT broker. It uses an MQTT login, not a Home Assistant token.

1. Add a broker login. Mosquitto add-on: Settings → Add-ons → Mosquitto broker → Configuration →
   Logins: add `username: jobalerts` and a password; restart the add-on. The MQTT integration must be
   set up.
2. Settings → Connections: MQTT host and user `jobalerts`. Settings → API keys: the password. (Or
   `MQTT_HOST`, `MQTT_USERNAME`, `MQTT_PASSWORD` in `.env`, then restart.)
3. Run `jobalerts top --push`. A "Job alerts" device appears with `sensor.job_alerts`:
   - state: number of matches;
   - `jobs`: top matches (last 7 days, by score then newest), each with `score`, `step_up`,
     `step_up_note`, `pay_index`, `pay_low`, `pay_high`, `buy_index`, `buy_low`, `buy_high`,
     `story_ids`, `best_resume`, `link`;
   - `step_ups`: the biggest step-ups, same fields.

   Updated every 5 minutes and after new matches. Messages are retained. The sensor becomes
   unavailable after 15 minutes without an update.
4. Add a Markdown card (Edit dashboard → Add card → Manual) and paste:

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

Step-ups card:

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

**AI cost:** `sensor.job_alerts_ai_cost_this_month` estimates this month's Claude spend at list
prices (attributes: `today`, `last_7_days`, `calls_month`, `calls_7_days`). It counts only jobalerts'
calls; your bill is on the Anthropic Console. `jobalerts stats` prints the same.

```yaml
type: entity
entity: sensor.job_alerts_ai_cost_this_month
name: Claude API cost this month
```

**Optional: restrict the MQTT login.** Set the Mosquitto add-on's `customize` option to
`active: true, folder: mosquitto` and create two files in `/share/mosquitto/`:

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

Every login must be listed once the file exists. Add a `user` block for each other login
(Zigbee2MQTT, etc.) and restart the add-on. Mosquitto silently drops publishes the list doesn't
allow; check this file if the sensor doesn't appear.

## Map

Web UI → Map. Each US metro is a dot colored by pay index compared with your current pay at home:
blue better, gray similar, red worse.

- **Pay index** at a salary ($100K–$400K slider), renting or buying.
- **Break-even salary:** what each metro must pay to match your current situation.
- **Jobs:** top matches (diamonds) and step-ups (rings). Jobs abroad or with unknown places are in
  *Table & details*.
- **Table & details:** each metro's numbers and the sources.

Dots use metro-wide figures; a job's own index also uses its city, so the two can differ. The page
refreshes every `homeassistant.interval`, after new matches and after a price data update.

**In Home Assistant:** turn on the share link in Settings → Account (`/map/embed?token=…`). The link
opens only the map; "Make a new link" replaces it. Add a Webpage card:

```yaml
type: iframe
url: http://<jobalerts-host>:8765/map/embed?token=<your token>&view=breakeven&housing=buy
aspect_ratio: 75%
title: Where the move pays
```

URL options: `view=index|breakeven`, `housing=rent|buy`, `salary=250000`, `jobs=0`,
`theme=dark|light`, `panel=1` (open the table).

The browser showing Home Assistant must reach `<jobalerts-host>:8765` and the internet (map tiles and
Leaflet load from public CDNs). If Home Assistant is on https, serve jobalerts over https too (e.g.
Caddy: `jobs.home.lan { reverse_proxy <jobalerts-host>:8765 }`), set `web.secure_cookies: true`
and use the https URL. `web.frame_origins` limits which sites can embed the map.

`jobalerts map --out FILE` writes the map to a file.
