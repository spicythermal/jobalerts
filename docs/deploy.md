# Deploy on a home server (Docker)

For a machine that runs all the time. Needs Docker with Compose 2.24 or newer (for the optional `.env`). For a desktop or laptop that's only on sometimes, see
[desktop-mode.md](desktop-mode.md).

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

Clone the repository on the server (your own fork or the shared one).

```sh
git clone <repository URL> jobalerts && cd jobalerts
cp .env.example .env && nano .env        # optional: keys can also go on the Settings page
mkdir -p data && chown 1000:1000 data   # the container runs as uid 1000 and writes only data/
docker compose up -d --build
docker compose logs -f                   # first polls record silent baselines
docker compose exec jobalerts jobalerts stats
# open http://<server>:8765/setup with the setup code from the log
```

Everything of yours lives in `data/` on the server (settings, profile, stories, resumes, watch
list, database, the vault key); change it on the web UI. `config/` is mounted read-only and only
changes with `git pull`.

### Updating

```sh
cd jobalerts && git pull
docker compose up -d --build             # rebuilds and recreates the container if the code changed
docker compose restart                   # after editing files in data/ by hand
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
Postings the employer has taken down since (HTTP 404, or Workday's 403 "permission denied") become
`closed` and leave `top`, Home Assistant and the digest. An unscored job that turns out to be closed
isn't sent to the AI. Other fetch errors are only logged, and the job keeps its status.

```sh
docker compose exec jobalerts jobalerts backfill --dry-run   # show the scores, save nothing
docker compose exec jobalerts jobalerts backfill             # save: 50+ go in the digest, `top` and HA show scores
docker compose exec jobalerts jobalerts backfill --push      # same, but 75+ are pushed to your phone now
docker compose exec jobalerts jobalerts backfill --rescore   # also AI-score again the matches already scored
```

`--rescore` is for after you change `stories/` or `profile.md`: every match already scored in the
window is scored again (~$0.001 each, so a few dimes for a full window) and gets the new score,
summary and top stories. It moves jobs between the digest and "low" to match the new score, but
never pushes, and a job already pushed or sent in a digest keeps its status. Restart the container
first so it reads the new stories.

Both `backfill` and `--rescore` share the daily AI budget (`scoring.daily_max_calls`, counted per UTC
day, so it resets at 4–5 PM Pacific) and stop when only `scoring.backfill_reserve` calls (150) are left,
so new postings can still be scored that day. Run them again after the reset to continue: `--rescore`
skips jobs already scored against the current stories and profile. If the budget does run out, new
matches go to the digest unscored instead of being pushed, and one phone notice says so.

### Automatic updates

`tools/update-docker.sh` updates this install from its git repository: it backs up the database,
fast-forwards, rebuilds, checks that the new version comes up, and goes back if it doesn't. Run it by
hand, or once a day with `contrib/jobalerts-update.service` + `.timer` (instructions inside them).
See [upgrading.md](upgrading.md).
