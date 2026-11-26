# Deploy on a home server (Docker)

Requires Docker with Compose 2.24 or newer. For a computer that isn't always on, see
[desktop-mode.md](desktop-mode.md).

## 1. Proxmox LXC only: enable nesting

On the Proxmox host (or Options → Features in the GUI):

```sh
pct set <CTID> --features nesting=1,keyctl=1   # keyctl: unprivileged containers only
pct reboot <CTID>
```

Not needed in a VM.

## 2. Install Docker (Debian / Ubuntu, as root)

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

To run `docker` without sudo: `usermod -aG docker <user>`, then log in again.

## 3. Run jobalerts

```sh
git clone https://github.com/spicythermal/jobalerts.git jobalerts && cd jobalerts
cp .env.example .env && nano .env        # optional; keys can be set on the Settings page instead
mkdir -p data && chown 1000:1000 data   # the container runs as uid 1000
docker compose up -d --build
docker compose logs jobalerts | grep "setup code"
```

Open `http://<server>:8765/setup` and enter the setup code.

Your data is in `data/`. `config/` is mounted read-only.

## Updating

```sh
git pull && docker compose up -d --build
```

After editing files in `data/` by hand: `docker compose restart`.

Automatic: `tools/update-docker.sh` backs up the database, pulls, rebuilds, checks the new version
and rolls back if the check fails. `contrib/jobalerts-update.service` and `.timer` run it daily
(instructions inside). See [upgrading.md](upgrading.md).

## Scoring jobs open before the first poll

The first poll of each board stores its open jobs without scoring them. `backfill` scores those that
pass the filter and are newer than `criteria.max_age_days`:

```sh
docker compose exec jobalerts jobalerts backfill --dry-run   # show scores, save nothing
docker compose exec jobalerts jobalerts backfill             # save; 50+ go to the digest
docker compose exec jobalerts jobalerts backfill --push      # also push 75+
docker compose exec jobalerts jobalerts backfill --rescore   # also re-score jobs already scored
```

`backfill` also re-checks the pay index of jobs already processed in that period, re-fetching each
description. Jobs below `pay.min_index` become `low_pay`; `low_pay` jobs above it return to the digest.
Jobs whose posting returns 404 (or Workday's 403) become `closed`.

`--rescore` re-scores after you change stories or the profile (about $0.001 per job). Scores move
jobs between the digest and "low"; nothing is pushed, and pushed or digested jobs keep their status.

Both stop when `scoring.backfill_reserve` (150) of the day's `scoring.daily_max_calls` remain. The
count resets at 00:00 UTC; run again to continue (scored jobs are skipped).
