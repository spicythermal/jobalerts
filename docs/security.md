# Security

jobalerts runs on your own machine and holds two kinds of private things: **API keys** (Anthropic,
ntfy, MQTT, the aggregators) and **personal numbers** (salary, home, housing cost, household,
email, profile, stories, resumes). This page says what protects them and where the limits are.

## Who can open the web UI

- One admin password. On first start jobalerts prints a one-time **setup code** to its log;
  `/setup` asks for it, so the first device to find the page on your network can't claim it.
- Passwords are stored as scrypt hashes. Wrong passwords back off per address (5 free tries, then
  30 s doubling up to 15 min). `jobalerts set-password` resets it from the machine itself.
- Sessions are HMAC-signed, HttpOnly, SameSite=Lax cookies (30 days). Changing the password logs
  out every other session. Set `web.secure_cookies: true` when you serve it over HTTPS.
- Every form and htmx request carries a CSRF token.
- The **Home Assistant map link** (`/map/embed?token=…`) opens the cost-of-living map and nothing
  else, read-only. Anyone with the link sees the map: pay indexes, break-even salaries (which
  reveal roughly what you earn) and your top jobs. "Make a new link" on Settings → Account retires
  the old one. `web.frame_origins` limits which sites may embed it.
- Pages are sent with a strict Content-Security-Policy (scripts only from the app itself; the map
  page also loads Leaflet and the state outlines from public CDNs), `nosniff` and `no-store`. Text
  from job boards is escaped, and a posting link is only rendered if it's http(s).

It's meant for your home network. To reach it from outside, put it behind a VPN (WireGuard,
Tailscale) or a reverse proxy with HTTPS; don't open the port to the internet. Behind a reverse
proxy every request comes from the proxy's address, so the login backoff applies to everyone at once.

## API keys: one way only

- Keys saved on Settings → API keys are encrypted (Fernet) in the database under a key derived
  from `data/secret.key` (created 0600 on first use), or from `JOBALERTS_SECRET_KEY` if set.
- The browser never gets a key back. The page shows only whether each key is set, where from
  (`.env` or saved here) and when. Key fields are always empty; a failed save doesn't echo what
  was typed.
- Only the running service reads keys. In the web code, only the **Test** buttons for Anthropic
  and USAJobs read one, to send it to that provider; the answer shown is redacted.
- A key in the real environment or `.env` wins over a saved one, and can only be changed there.
- Logs are filtered: known key values and key-like URL parameters (`app_key=…`, `token=…`) are
  replaced with `[redacted]`, including in error tracebacks.
- The tests check all of this: every page, error page and failed form, the MQTT payload, the map,
  the logs and the raw database file are searched for planted test keys
  (`tests/test_security.py`).

## Personal numbers

- Values from Settings → Your situation (salary, home, housing cost, household, contact email) are
  encrypted in the database with the same key. Thresholds and word lists are stored plain.
- `data/config.local.yaml` is plain text. If it holds personal values (an install upgraded from
  before the web UI), Settings → Your situation offers to move them into encrypted storage.
- Your profile, stories and resumes are plain files in `data/` (the AI needs to read them, and you
  may edit them elsewhere). They're never committed: `data/` is gitignored.
- The AI scorer sends your profile, stories, resume openings, your stay-or-move preference and the
  place you live to Anthropic's API with each scoring call. Nothing else leaves the machine except
  what you configure: ntfy pushes (job titles, scores, pay notes) and the Home Assistant sensor.

## What the encryption does and doesn't do

The key sits next to the database, because the service has to read your keys to use them. So the
encryption protects a copy or backup of the database on its own; it does **not** protect against
someone who can read the whole `data/` folder or log in to the machine. Keep `data/` private, and
back up `data/secret.key` with the database: without it, saved keys and encrypted settings can't
be read (set them again).

## Reporting a problem

Tell the maintainer directly, not in a public issue, if you find a way to read a key or another
user's data.
