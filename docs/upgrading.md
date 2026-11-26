# Updating without losing your changes

An update replaces the program and the shipped defaults; it never touches what's yours.

## What's yours, and why an update leaves it alone

Everything you change lives in `data/` (and, if you put them there, folders git ignores):

| Yours | Where | How an update treats it |
|---|---|---|
| Settings from the Settings page | `data/jobalerts.db` (`settings` table; personal values encrypted) | Kept. Only values you changed are stored, so new defaults still reach you. |
| API keys | `data/jobalerts.db` (`secrets`), key in `data/secret.key` | Kept. |
| Your file overrides | `data/config.local.yaml` | Kept; merged on top of the new `config/config.yaml`. |
| Watch list, profile, stories, resumes | `data/companies.yaml`, `data/profile.md`, `data/stories/`, `data/resumes/` | Kept. Examples are only copied in when a file is missing. |
| Jobs, scores, saved/dismissed/applied, board health | `data/jobalerts.db` | Kept; upgrades only add to it. |

Git ignores `data/`, so `git pull` can't change or delete anything in it. Docker mounts `data/` from
the host and `config/` read-only, so rebuilding or recreating the container doesn't either.

## When a new version changes a setting

- **Renamed setting:** your value moves to the new name (`RENAMED` in `src/jobalerts/userdata.py`
  lists them), in `config.local.yaml` and on the Settings page alike.
- **Removed setting, or a value the new version doesn't accept:** that one value is set aside and
  everything else of yours stays in use. jobalerts still starts. The Settings pages list what was
  set aside, and so does the log. Values saved on the Settings page are kept in the database even
  then, so going back to the earlier version restores them.

## When a new version changes the database

Before an upgrade changes stored data, jobalerts copies the database to
`data/backups/jobalerts-<time>-before-schema-<n>.db` (the last five are kept). To go back, stop
jobalerts, copy a backup over `data/jobalerts.db`, and start the earlier version. A database
written by a newer version is opened without changes (with a warning in the log).

## How to update

```sh
git pull
docker compose up -d --build        # Docker
pip install -e .                    # or a Python install (then restart `jobalerts run`)
```

Back up `data/` now and then (it holds everything of yours, including `secret.key`).

## How you could still lose something

- **Editing shipped files** (`config/config.yaml`, `config/taxes.yaml`, `src/`): `git pull` then
  stops with "your local changes would be overwritten". Undoing that with `git checkout` or
  `git reset --hard` throws the edit away. Put changes in `data/config.local.yaml` or on the
  Settings page instead.
- **`git clean -fdx`** deletes ignored files too: all of `data/`. Never run it in the jobalerts folder.
- **Losing `data/secret.key`:** saved keys and personal settings can't be read any more; set them again.
- **Installs from before 2026-11-24** kept your files in the repository. Run
  `tools/migrate_user_data.sh` once before pulling (see the README).
