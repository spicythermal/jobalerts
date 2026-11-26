# Running it on a desktop or laptop

You don't need a server. Run jobalerts on the computer you use, start it at login, and let it
catch up whenever the computer was off or asleep. Nothing to switch: it's the same `jobalerts run`.

## What happens after a gap

The service writes a heartbeat to its database every minute. When it starts, or wakes up and
finds the clock jumped, more than 45 minutes after the last heartbeat (`run.catchup_after`), it:

1. starts the web UI right away;
2. polls every board at once (still paced per host);
3. widens the aggregator searches back to their last successful poll (Adzuna up to 30 days,
   USAJobs up to 60, JSearch up to a month, TheirStack since the last poll);
4. runs the AI pre-screen over the jobs the keywords turned down;
5. sends **one** summary push ("Catch-up: 14 new matches since Mon 18:02 (3 strong)") and the
   best 3 jobs (`run.catchup_top_pushes`) as their own pushes;
6. keeps the rest in the evening digest and shows a "Since you were away" report on the jobs page
   for 24 hours (`run.report_hours`);
7. sends a digest that came due while it was off, once;
8. goes back to normal polling.

What it can't recover: a job posted and taken down while the computer was off is never seen, and
a board that lists only its newest jobs (Workday shows one page) may have moved past some.
TheirStack charges a credit per job, so a long gap costs more of them (each search's `limit` still
caps one poll).

## Start it at login

**Linux (systemd):** `contrib/jobalerts.service` is a user service. Copy it, check the two paths
in it, and enable it:

```sh
mkdir -p ~/.config/systemd/user && cp contrib/jobalerts.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now jobalerts
journalctl --user -u jobalerts -f          # the log; the first-run setup code is printed here
```

`loginctl enable-linger $USER` keeps it running while you're logged out.

**Windows:** from the jobalerts folder, after installing it into `.venv`:

```powershell
powershell -ExecutionPolicy Bypass -File contrib\jobalerts-startup.ps1
```

It registers a Task Scheduler task that runs `jobalerts run` hidden at logon and logs to
`data\jobalerts.log`. Remove it with `Unregister-ScheduledTask -TaskName jobalerts`.

**Docker Desktop / Podman Desktop:** `docker compose up -d` with `restart: unless-stopped` (as
shipped) starts with the container engine; turn on "start at login" in its settings.

## Good to know

- Only one `jobalerts run` can use a database at a time. A second one stops with "already
  running". `jobalerts web` (the web UI alone) can run alongside, but settings saved there only
  reach the running service after it restarts; use the web UI that `run` serves instead.
- Alerts can only open the job on your computer while it's on. Set Settings → Connections →
  address to something your phone can reach (the PC's LAN address), or leave it empty to open
  the employer's posting instead.
- Moving between a server and a desktop: copy `data/` (with `secret.key`) across, and run only one
  of them at a time.
