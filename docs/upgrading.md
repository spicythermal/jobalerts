# Updating

Updates replace the program and `config/`. They don't change `data/`.

## Your data

| Data | Location | On update |
|---|---|---|
| Settings page values | `data/jobalerts.db` (`settings_by_user`; personal values encrypted) | Kept. Only changed values are stored, so new defaults apply to the rest. |
| API keys | `data/jobalerts.db` (`secrets_by_user`); key in `data/secret.key` | Kept. |
| Accounts | `data/jobalerts.db` (`users`) | Kept. |
| File overrides | `data/config.local.yaml` | Kept; merged over the new `config/config.yaml`. |
| Watch list, profile, stories, resumes | `data/companies.yaml`, `data/profile.md`, `data/stories/`, `data/resumes/`; other users' in `data/users/<id>/` | Kept. Examples are copied only when a file is missing. |
| Jobs, scores, job states, board health | `data/jobalerts.db` | Kept. |

Git ignores `data/`. Docker mounts `data/` from the host and `config/` read-only.

## Changed settings

- **Renamed:** values move to the new name (`RENAMED` in `src/jobalerts/userdata.py`).
- **Removed or invalid:** that value is ignored and listed on the Settings pages and in the log;
  everything else applies. Values saved on the Settings page stay in the database, so an older
  version can use them again.

## Database changes

Before changing stored data, an update copies the database to
`data/backups/jobalerts-<time>-before-schema-<n>.db` (last five kept). To go back: stop jobalerts, copy
the backup over `data/jobalerts.db`, start the older version. A database from a newer version is
opened unchanged, with a warning.

### Accounts (schema 3)

The update that added accounts turns the single password into the account `admin` (the first admin):
log in with username `admin` and your old password. Everyone is logged out once. Your jobs, scores,
saved jobs, settings and keys become that account's. The old tables are left as they were, so the
previous version still runs on the database if you go back (without what changed since).

## Python install (git checkout)

The service checks the repository daily. Settings → Updates (`update.mode`): *Tell me* (default:
one push per new version, and a line in the digest), *Install it*, or *Don't check*.
`jobalerts update` installs now; `jobalerts update --check` shows what's new.

Installing:

1. stops if shipped files were edited or there are local commits;
2. moves files from older installs into `data/`;
3. backs up the database;
4. fast-forwards and reinstalls the package;
5. runs `jobalerts check`; if it fails, returns to the previous version;
6. restarts `jobalerts run` (on Windows, the logon task restarts it).

## Docker

On the host, `tools/update-docker.sh` (`--check` to only look) does the same with `docker compose`:
backup, pull, rebuild, wait for `/healthz` and `jobalerts check`, roll back on failure.
`contrib/jobalerts-update.service` and `.timer` run it daily (instructions inside). For other setups:
`COMPOSE="podman compose"`, `HEALTH_URL=…`.

## By hand

```sh
git pull
docker compose up -d --build        # Docker
pip install -e .                    # Python; then restart jobalerts run
```

## Ways to lose data

- **Editing shipped files** (`config/`, `src/`): `git pull` stops. `git checkout` or `git reset --hard`
  then discards the edit. Use `data/config.local.yaml` or the Settings page instead.
- **`git clean -fdx`** deletes `data/`.
- **Losing `data/secret.key`:** saved keys and personal settings can't be decrypted.
- **Installs from before 2026-11-24:** run `tools/migrate_user_data.sh` before pulling (see README).
- **Installs cloned from the earlier repository:** run `tools/switch-to-public.sh` (see README).

Back up `data/` regularly.
