# Security

jobalerts stores API keys (Anthropic, ntfy, MQTT, aggregators) and personal data (salary, home,
housing cost, household, email, profile, stories, resumes).

## Web UI access

- One admin password. The first start prints a one-time setup code to the log; `/setup` requires it.
- Passwords are stored as scrypt hashes. After 5 wrong passwords from one address, logins are delayed
  30 seconds, doubling up to 15 minutes. `jobalerts set-password` resets the password.
- Sessions: HMAC-signed, HttpOnly, SameSite=Lax cookies, valid 30 days. Changing the password ends
  other sessions. Set `web.secure_cookies: true` when serving over HTTPS.
- Every form and htmx request requires a CSRF token.
- Headers: Content-Security-Policy (scripts from the app only; the map page also loads Leaflet and
  state outlines from public CDNs), `nosniff`, `no-store`. Job board text is escaped; only http(s)
  links are rendered.
- Pages that work without a login: `/login`, `/setup`, `/healthz`, static files and the map link.
  Others redirect to the login or return 401/404. `tests/test_upgrades.py` checks every page as a
  logged-out visitor for planted personal values.

**Map link** (`/map/embed?token=…`): off until turned on in Settings → Account. It uses a 256-bit
random token and opens only the map. It shows your home metro, each metro's pay index and break-even
salary (your current salary at your home metro), and your top jobs. It doesn't show housing costs,
mortgage or household. "Make a new link" replaces the token; "Turn the link off" removes it.
`web.frame_origins` limits which sites can embed it.

The web UI is for your home network. For outside access, use a VPN or a reverse proxy with HTTPS;
don't expose the port to the internet. Behind a reverse proxy all requests share the proxy's
address, so the login delay applies to everyone.

## API keys

- Keys saved in Settings → API keys are encrypted (Fernet) in the database with a key derived from
  `data/secret.key` (mode 0600, created on first use) or `JOBALERTS_SECRET_KEY`.
- The browser never receives a key. The page shows only whether each key is set, its source (`.env`
  or Settings) and when it was saved. Key fields are always empty, including after a failed save.
- The service reads keys to use them. In the web code, only the Test buttons (Anthropic, USAJobs)
  read a key, to call that provider; the result shown is redacted.
- A key in the environment or `.env` overrides a saved key and can only be changed there.
- Logs replace known key values and key-like URL parameters (`app_key=`, `token=`) with `[redacted]`,
  including in tracebacks.
- `tests/test_security.py` searches every page, error page, failed form, MQTT payload, the map, the
  logs and the raw database file for planted test keys.

## Personal data

- Settings → Your situation values (salary, home, housing cost, household, contact email) are
  encrypted in the database with the same key. Other settings are stored plain.
- `data/config.local.yaml` is plain text. If it holds personal values, Settings → Your situation
  offers to move them into encrypted storage.
- Profile, stories and resumes are plain files in `data/`, which git ignores.
- Pushes, the digest and the Home Assistant sensor include job titles, scores and pay notes. Pay notes
  don't include your salary or housing costs. The full calculation is shown only on a job's page
  (logged in) and by `jobalerts pay`.
- Each AI call sends Anthropic your profile, stories, resume openings, stay-or-move preference and
  home location.

## Limits of the encryption

The key is stored next to the database, because the service needs to read the keys. The encryption
protects a copy of the database alone. It does not protect against someone who can read `data/` or
log in to the machine. Keep `data/` private and back up `secret.key` with the database; without it,
saved keys and encrypted settings are lost.

## Reporting a vulnerability

Report it privately to the maintainer, not in a public issue.
