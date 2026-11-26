#!/bin/sh
# Move an older jobalerts install (a git checkout of the earlier, private repository, run with Docker)
# to the public repository, keeping everything of yours.
#
# Run it on the server, in the jobalerts folder:
#
#   git fetch https://github.com/spicythermal/jobalerts.git main && git show FETCH_HEAD:tools/switch-to-public.sh | sh
#
# What it does, stopping at the first problem:
#   1. a full archive of this folder (your data, .env, settings, stories, resumes, everything) next to it;
#   2. stops the container, so the database is copied consistently;
#   3. copies your files into data/ (tools/migrate_user_data.sh: never overwrites) and checks every one
#      arrived intact;
#   4. switches the code to the public repository (only shipped files change; data/ and .env don't);
#   5. rebuilds and starts it, and waits for it to answer and pass `jobalerts check`. On first start your
#      old settings become data/config.local.yaml and the database is upgraded (with its own backup).
# If 5 fails it puts the old code back and starts that again. Either way it prints how to undo everything.
#
# Settings: PUBLIC_URL (the public repository), COMPOSE (default "docker compose"), HEALTH_URL.
set -eu
PUBLIC_URL=${PUBLIC_URL:-https://github.com/spicythermal/jobalerts.git}
COMPOSE=${COMPOSE:-docker compose}
HEALTH_URL=${HEALTH_URL:-http://127.0.0.1:8765/healthz}
say() { printf '\n== %s\n' "$*"; }
die() { printf '\nStopped: %s\nNothing of yours was changed after the backup; see above.\n' "$*" >&2; exit 1; }

[ -d .git ] && [ -f docker-compose.yml ] && [ -d config ] || die "run this in the jobalerts folder (with docker-compose.yml)"
folder=$(pwd)
old=$(git rev-parse HEAD)
old_url=$(git config --get remote.origin.url || echo "")
stamp=$(date +%Y%m%d-%H%M%S)
backup="$folder/../jobalerts-before-public-$stamp.tar.gz"

say "1. Backing up this whole folder to $backup"
tar -czf "$backup" -C "$folder/.." "$(basename "$folder")"
tar -tzf "$backup" >/dev/null || die "the backup archive can't be read"
echo "   $(du -h "$backup" | cut -f1), commit $old"

say "2. Stopping jobalerts"
$COMPOSE stop || echo "   (it wasn't running)"

say "3. Fetching the public version and copying your files into data/"
git fetch --quiet "$PUBLIC_URL" main
new=$(git rev-parse FETCH_HEAD)
# Your files as they are now (the migration puts tracked ones back to their committed version after copying).
snap=$(mktemp -d)
trap 'rm -rf "$snap"' EXIT
for p in config/profile.md config/companies.yaml stories resume_reference; do
  if [ -e "$p" ]; then mkdir -p "$snap/$(dirname "$p")" && cp -Rp "$p" "$snap/$p"; fi
done
git diff --binary HEAD > "$snap/local-edits.patch"   # edits to shipped files, put back if we roll back
git show "$new:tools/migrate_user_data.sh" | sh
# Every file of yours from the old locations must now be in data/, byte for byte.
check_copy() {  # check_copy <old file> <new file>: fine if the old one doesn't exist
  if [ ! -e "$1" ]; then return 0; fi
  if cmp -s "$1" "$2"; then return 0; fi
  echo "   missing or different: $2 (from $1)" >&2
  return 1
}
for f in "$snap"/stories/*.md; do
  if [ -e "$f" ] && [ "$(basename "$f")" != README.md ]; then
    check_copy "$f" "data/stories/$(basename "$f")" || die "a story didn't copy"
  fi
done
for f in "$snap"/resume_reference/*; do
  if [ -e "$f" ]; then check_copy "$f" "data/resumes/$(basename "$f")" || die "a resume didn't copy"; fi
done
check_copy "$snap/config/profile.md" data/profile.md || die "profile.md didn't copy"
check_copy "$snap/config/companies.yaml" data/companies.yaml || die "companies.yaml didn't copy"
[ -e data/config.local.yaml ] || [ -e data/config.migrate.yaml ] || die "your config.yaml wasn't kept"
echo "   all your files are in data/"

say "4. Switching the code to the public repository ($(git rev-parse --short "$new"))"
git remote set-url origin "$PUBLIC_URL"
git fetch --quiet origin
git reset --hard --quiet origin/main
git branch --quiet --set-upstream-to=origin/main 2>/dev/null || true

say "5. Building and starting the new version"
healthy() {
  i=0
  while [ $i -lt 60 ]; do
    if curl -fsS "$HEALTH_URL" >/dev/null 2>&1 && $COMPOSE exec -T jobalerts jobalerts check; then return 0; fi
    i=$((i + 1)); sleep 2
  done
  return 1
}
if $COMPOSE build --build-arg JOBALERTS_VERSION="$(git rev-parse --short HEAD)" && $COMPOSE up -d && healthy; then
  say "Done: jobalerts runs the public version $(git rev-parse --short HEAD)"
  echo "   Set the web UI password with the setup code from the log:"
  echo "     $COMPOSE logs jobalerts | grep 'setup code'      then open http://<this machine>:8765/setup"
  echo "   Updates from now on: tools/update-docker.sh (daily with contrib/jobalerts-update.timer)."
  echo "   Your backup: $backup (keep it until you've checked everything)."
  exit 0
fi

say "The new version didn't come up; going back to the old code"
[ -n "$old_url" ] && git remote set-url origin "$old_url"
git reset --hard --quiet "$old"
if [ -s "$snap/local-edits.patch" ]; then
  git apply "$snap/local-edits.patch" || echo "   Couldn't re-apply your edits to shipped files; they're in data/ and the backup." >&2
fi
$COMPOSE build && $COMPOSE up -d || true
echo "   Running the old code again (commit $old). Your files are as they were, plus copies in data/."
echo "   To undo completely: $COMPOSE down && cd .. && rm -rf '$folder' && tar -xzf '$backup'"
exit 1
