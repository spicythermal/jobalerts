#!/bin/sh
# Update a Docker install of jobalerts from its git repository. Run it on the host, in the jobalerts
# folder (contrib/jobalerts-update.timer runs it once a day):
#
#   tools/update-docker.sh            # update if there's a new version
#   tools/update-docker.sh --check    # only show what's new
#
# Like `jobalerts update`: fast-forward only, and it refuses when shipped files were changed or there
# are local commits, so nothing of yours is overwritten (yours is in data/, which git ignores). Before
# updating it backs up the database through the running container; after rebuilding it waits for the
# new version to answer and pass `jobalerts check`, and goes back to the previous version if it doesn't.
# Exit code 0: updated or up to date; 1: refused or rolled back (the reason is printed).
set -eu
cd "$(dirname "$0")/.."
COMPOSE=${COMPOSE:-docker compose}                                   # e.g. COMPOSE="podman compose"
HEALTH_URL=${HEALTH_URL:-http://127.0.0.1:8765/healthz}

git fetch --quiet
upstream=$(git rev-parse --abbrev-ref --symbolic-full-name '@{u}')
behind=$(git rev-list --count 'HEAD..@{u}')
if [ "$behind" -eq 0 ]; then
  echo "Up to date ($(git rev-parse --short HEAD))."
  exit 0
fi
echo "$behind new change(s) on $upstream ($(git rev-parse --short HEAD) -> $(git rev-parse --short '@{u}')):"
git log --format='  %h %s' -n 20 'HEAD..@{u}'
[ "${1:-}" = "--check" ] && exit 0

if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  echo "Not updating: shipped files were changed here. Put your changes in data/ (or commit them), then update by hand:"
  git status --short --untracked-files=no
  exit 1
fi
if [ "$(git rev-list --count '@{u}..HEAD')" -ne 0 ]; then
  echo "Not updating: there are commits here that aren't on $upstream. Update by hand."
  exit 1
fi

old=$(git rev-parse HEAD)
if [ -n "$(git ls-files stories resume_reference relocation config/profile.md config/companies.yaml config/candidates.txt)" ]; then
  echo "Moving your files into data/ first (an install from before 2026-11-24):"
  git show '@{u}:tools/migrate_user_data.sh' | sh
fi
$COMPOSE exec -T jobalerts jobalerts backup || echo "(no running container to back up from; continuing)"
git merge --ff-only --quiet '@{u}'

build() {
  $COMPOSE build --build-arg JOBALERTS_VERSION="$(git rev-parse --short HEAD)" && $COMPOSE up -d
}
healthy() {
  i=0
  while [ $i -lt 60 ]; do
    if curl -fsS "$HEALTH_URL" >/dev/null 2>&1 && $COMPOSE exec -T jobalerts jobalerts check >/dev/null 2>&1; then
      return 0
    fi
    i=$((i + 1)); sleep 2
  done
  return 1
}

if build && healthy; then
  echo "Updated to $(git rev-parse --short HEAD)."
  exit 0
fi
echo "The new version didn't come up; going back to $(git rev-parse --short "$old")."
git reset --hard --quiet "$old"
build || true
exit 1
