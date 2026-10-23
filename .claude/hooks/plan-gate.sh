#!/usr/bin/env bash
# Stop hook: keep Claude working while PLAN.md has unchecked "- [ ]" tasks.
# Lets Claude stop when: PLAN.md missing, all tasks done, a "BLOCKED:" line exists,
# the kill switch .claude/STOP_AUTORUN exists, or no files changed across MAX_IDLE stops.

dir="${CLAUDE_PROJECT_DIR:-$(pwd)}"
plan="$dir/PLAN.md"
state="$dir/.claude/.plan-gate-count"
marker="$dir/.claude/.plan-gate-marker"
MAX_IDLE=5

cat >/dev/null  # drain hook input

allow() { rm -f "$state" "$marker"; exit 0; }

[ -f "$dir/.claude/STOP_AUTORUN" ] && allow
[ -f "$plan" ] || allow
grep -qE '^[[:space:]]*BLOCKED:' "$plan" && allow

remaining=$(grep -cE '^[[:space:]]*- \[ \]' "$plan")
[ "$remaining" -eq 0 ] && allow

# Progress guard: count consecutive stops where no project file changed.
count=0
if [ -f "$marker" ] && [ -f "$state" ]; then
  changed=$(find "$dir" -type f -newer "$marker" -not -path '*/.git/*' -not -path '*/.claude/*' \
    -not -path '*/.venv/*' -not -path '*/__pycache__/*' -print -quit 2>/dev/null)
  if [ -z "$changed" ]; then count=$(( $(cat "$state") + 1 )); fi
fi
if [ "$count" -ge "$MAX_IDLE" ]; then
  echo "{\"systemMessage\": \"plan-gate: no file changes across $MAX_IDLE stops; letting Claude stop ($remaining tasks unchecked).\"}"
  allow
fi
echo "$count" > "$state"
touch "$marker"

next=$(grep -m1 -E '^[[:space:]]*- \[ \]' "$plan" | sed -e 's/^[[:space:]]*- \[ \] *//' -e 's/["\\]/ /g')
echo "{\"decision\": \"block\", \"reason\": \"PLAN.md still has $remaining unchecked task(s). Continue autonomously per CLAUDE.md. Next: $next. If a task needs the user, mark it [~] with the reason and move on; if everything left is blocked, add a 'BLOCKED: <reason>' line to PLAN.md.\"}"
exit 0
