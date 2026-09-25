#!/bin/bash
# SessionStart backstop (project-local, committed). Catches the case where
# the previous editor session never reached the Stop hook at all -- a hard
# interrupt, a crash, a compaction mid-task. Independently re-runs the same
# verifier from scratch rather than trusting any log the previous session
# might have written before it stopped short; a log written only by the
# stage that failed to run is empty in exactly the case this backstop
# exists for. A fresh session is never blocked from starting here, only
# warned: the goal is "seen on the very first turn", not "prevented".
set -u

input=$(cat)

# jq is the normal path for reading the hook JSON; if it is missing from
# PATH, fall back to python3's stdlib json rather than let cwd/sid degrade
# to "" -- the card still needs a real session id to key the state file on.
if command -v jq >/dev/null 2>&1; then
  cwd=$(printf '%s' "$input" | jq -r '.cwd // empty')
  sid=$(printf '%s' "$input" | jq -r '.session_id // "unknown"')
else
  cwd=$(printf '%s' "$input" | python3 -c '
import json, sys
try:
    print(json.load(sys.stdin).get("cwd") or "")
except Exception:
    print("")
')
  sid=$(printf '%s' "$input" | python3 -c '
import json, sys
try:
    print(json.load(sys.stdin).get("session_id") or "unknown")
except Exception:
    print("unknown")
')
fi
[ -n "$cwd" ] && cd "$cwd" 2>/dev/null

git rev-parse --is-inside-work-tree >/dev/null 2>&1 || exit 0
root=$(git rev-parse --show-toplevel)
status_file="$root/STATUS.yaml"

# The system python lacks the project's own deps (pyyaml, etc.) -- running
# the verifier under it reports false FAILs on import, not real drift.
py="${GT_PYTHON:-}"
[ -z "$py" ] && { [ -x "$root/.venv/bin/python" ] && py="$root/.venv/bin/python" || py="python3"; }

# Memory covers committed changes and sibling repositories too. No model calls or writes.
memory="$root/tools/ground_truth/gt_context.py"
[ -f "$memory" ] && "$py" "$memory" hook --event start
[ -f "$status_file" ] || exit 0
[ -f "$root/tools/ground_truth/verify.py" ] || exit 0

# A restart after compaction or a crash resets the session's acks and red
# streak on purpose: the covered-file gate should not trust a blast_radius
# read from a turn the model no longer remembers.
state="$root/tools/ground_truth/gt_hook_state.py"
[ -f "$state" ] && "$py" "$state" --root "$root" session-start "$sid" >/dev/null 2>&1

# Read enforcement the same way the Stop hook does.
mode=$("$py" -c "
import sys
import yaml
try:
    data = yaml.safe_load(open(sys.argv[1])) or {}
    print((data.get('meta') or {}).get('enforcement', 'advisory'))
except Exception:
    print('advisory')
" "$status_file" 2>/dev/null)
[ -z "$mode" ] && mode=advisory

cat <<CARDEOF
## ground-truth contract active (enforcement: $mode)
- Understand affected behavior and consumers; inspect the diff and run relevant tests. STATUS.yaml covers only its declared scope.
- Update claims only when their meaning, path or evidence changes. A status raise needs green baseline, red mutation, restored green evidence.
- Skipped or incomplete checks are not proof. Repeated failure: diagnose and report, do not weaken the check.
- Pre-edit gates are opt-in (GT_STRICT_EDIT_GUARD=1). Details: .claude/rules/ground-truth.md
CARDEOF

out=$(cd "$root" && "$py" tools/ground_truth/verify.py --mode=sync --show-warn 2>&1)
rc=$?
if [ $rc -eq 0 ]; then
  warn_out=$(printf '%s\n' "$out" | grep '^\[WARN\]')
  if [ -n "$warn_out" ]; then
    n=$(printf '%s\n' "$warn_out" | wc -l | tr -d ' ')
    echo "## ground-truth: $n warnings in STATUS.yaml (not blocking; inspect relevant warnings)"
    printf '%s\n' "$warn_out" | head -3
    [ "$n" -gt 3 ] && echo "... $((n - 3)) more: $py tools/ground_truth/verify.py --mode=sync --show-warn"
  fi
  exit 0
fi

echo "## ground-truth: STATUS.yaml is currently out of sync"
echo '```'
echo "$out"
echo '```'
exit 0
