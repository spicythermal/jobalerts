# Running on a desktop or laptop

Run `jobalerts run` at login. It catches up after the computer was off or asleep.

## After a gap

The service writes a heartbeat every minute. If it starts, or wakes from sleep, more than 45 minutes
(`run.catchup_after`) after the last heartbeat, it:

1. starts the web UI;
2. polls every board (with per-host pacing);
3. searches the aggregators back to their last successful poll (Adzuna up to 30 days, USAJobs up to
   60, JSearch up to a month, TheirStack since the last poll);
4. runs the AI pre-screen on queued jobs;
5. sends one summary push and the top 3 jobs (`run.catchup_top_pushes`) as separate pushes;
6. puts the rest in the digest and shows a "Since you were away" report on the Jobs page for
   24 hours (`run.report_hours`);
7. sends a missed digest once;
8. resumes normal polling.

Not recovered: jobs posted and removed while it was off, and jobs that scrolled off boards that list
only their newest jobs (Workday shows one page). A long gap uses more TheirStack credits (each
search's `limit` caps one poll).

## Start at login

**Linux (systemd):** edit the two paths in `contrib/jobalerts.service`, then:

```sh
mkdir -p ~/.config/systemd/user && cp contrib/jobalerts.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now jobalerts
journalctl --user -u jobalerts -f          # log, including the setup code
```

To keep it running while logged out: `loginctl enable-linger $USER`.

**Windows:** in the jobalerts folder, after installing into `.venv`:

```powershell
powershell -ExecutionPolicy Bypass -File contrib\jobalerts-startup.ps1
```

This registers a scheduled task that runs `jobalerts run` at logon, logging to `data\jobalerts.log`.
Remove it: `Unregister-ScheduledTask -TaskName jobalerts`.

**Docker Desktop / Podman Desktop:** `docker compose up -d`; the container restarts with the engine
(`restart: unless-stopped`). Enable the engine's start-at-login setting.

## Notes

- One `jobalerts run` per database; a second one exits with "already running". `jobalerts web` can
  run alongside it, but settings saved there reach the service only after a restart.
- Alerts link to this computer's web UI only if Settings → Connections has an address your phone can
  reach. If it's empty, alerts link to the employer's posting.
- To move between machines, copy `data/` (including `secret.key`) and run only one at a time.
