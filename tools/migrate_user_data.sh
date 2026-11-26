#!/bin/sh
# Move an older jobalerts install's own files into data/ before pulling the version that stopped
# tracking them in git (that pull deletes config/profile.md, config/companies.yaml, stories/ and
# resume_reference/ from the working tree).
#
# Run it in the jobalerts folder, then pull and restart:
#   git fetch && git show origin/main:tools/migrate_user_data.sh | sh
#   git pull && docker compose up -d --build
#
# It never overwrites a file already in data/. Your old config.yaml is kept as
# data/config.migrate.yaml; the next start turns it into data/config.local.yaml (only the
# settings that differ from the new defaults).
set -eu
[ -f config/config.yaml ] && [ -d .git ] || { echo "run this in the jobalerts folder" >&2; exit 1; }

mkdir -p data/stories data/resumes data/private
copy() {  # copy <from> <to>: only if <from> exists and <to> doesn't (never overwrites)
  if [ -e "$1" ] && [ ! -e "$2" ]; then cp -Rp "$1" "$2" && echo "  $1 -> $2"; fi
}

echo "Copying your files into data/:"
copy config/profile.md data/profile.md
copy config/companies.yaml data/companies.yaml
copy config/candidates.txt data/private/candidates.txt
[ -e data/config.local.yaml ] || copy config/config.yaml data/config.migrate.yaml
[ -e data/config.local.yaml ] || copy config/taxes.yaml data/taxes.migrate.yaml   # its household
for f in stories/*.md; do
  if [ -e "$f" ] && [ "$(basename "$f")" != README.md ]; then copy "$f" "data/stories/$(basename "$f")"; fi
done
copy stories/talks data/private/talks
for f in resume_reference/*; do
  if [ -e "$f" ]; then copy "$f" "data/resumes/$(basename "$f")"; fi
done
copy relocation data/private/relocation

# The copies are safe in data/; put the tracked files back as committed so `git pull` can go ahead.
git checkout -- config/config.yaml config/taxes.yaml 2>/dev/null || true
for p in config/profile.md config/companies.yaml config/candidates.txt stories resume_reference relocation; do
  if git ls-files --error-unmatch "$p" >/dev/null 2>&1; then git checkout -- "$p"; fi
done
echo "Done. Next: git pull && docker compose up -d --build"
