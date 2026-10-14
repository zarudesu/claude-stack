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

# Emergency switch: GT_HOOKS_OFF=1 turns every ground-truth hook off, and
# says so on each run instead of going quiet.
if [ "${GT_HOOKS_OFF:-0}" = "1" ]; then
  echo '{"systemMessage":"ground-truth hooks are off (GT_HOOKS_OFF=1)"}'
  exit 0
fi

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
# The contract: legacy STATUS.yaml or sharded .ground-truth/contract.yaml.
has_contract() { [ -f "$root/STATUS.yaml" ] || [ -f "$root/.ground-truth/contract.yaml" ]; }
name="STATUS.yaml"
[ -f "$root/STATUS.yaml" ] || name="contract (.ground-truth/)"

# The system python lacks the project's own deps (pyyaml, etc.) -- running
# the verifier under it reports false FAILs on import, not real drift.
# Order: GT_PYTHON, the absolute path the installer pinned in the git dir,
# .venv, python3 from PATH. Whichever it is must import PyYAML; a broken one
# is reported below, never skipped.
py="${GT_PYTHON:-}"
# Per-worktree git dir first, then the common one (an install from the main
# clone pins there for every linked worktree); made absolute by hand (git
# <2.31 has no --path-format).
for gd in "$(git rev-parse --git-dir)" "$(git rev-parse --git-common-dir)"; do
  case "$gd" in /*) ;; *) gd="$PWD/$gd" ;; esac
  [ -z "$py" ] && [ -s "$gd/ground-truth-python" ] && py=$(cat "$gd/ground-truth-python")
done
[ -z "$py" ] && { [ -x "$root/.venv/bin/python" ] && py="$root/.venv/bin/python" || py="python3"; }

# Memory covers committed changes and sibling repositories too. No model calls or writes.
memory="$root/tools/ground_truth/gt_context.py"
{ [ -f "$memory" ] || { has_contract && [ -f "$root/tools/ground_truth/verify.py" ]; }; } || exit 0
if ! env_err=$("$py" -c 'import yaml' 2>&1); then
  echo "## ground-truth: environment broken: $(printf '%s\n' "$env_err" | tail -1); run: python3 tools/ground_truth/gt_doctor.py"
  exit 0
fi
# Lines that change with the repo state are printed after the static card,
# sorted, with absolute dates, so the start of the context keeps the same
# bytes from session to session while nothing changes.
dyn=$(
  [ -f "$memory" ] && "$py" "$memory" hook --event start
  # One line about deferred drift, only when the queue is not empty.
  pending="$root/tools/ground_truth/gt_pending.py"
  [ -f "$pending" ] && "$py" "$pending" --root "$root" summary
)
show_dyn() { [ -n "$dyn" ] && printf '%s\n' "$dyn" | LC_ALL=C sort; }
has_contract || { show_dyn; exit 0; }
[ -f "$root/tools/ground_truth/verify.py" ] || { show_dyn; exit 0; }

# A restart after compaction or a crash resets the session's acks and red
# streak on purpose: the covered-file gate should not trust a blast_radius
# read from a turn the model no longer remembers.
state="$root/tools/ground_truth/gt_hook_state.py"
[ -f "$state" ] && "$py" "$state" --root "$root" session-start "$sid" >/dev/null 2>&1

# Read enforcement the same way the Stop hook does; both layouts on disk
# is reported here and blocks at Stop.
if ! mode=$("$py" -c "
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1] + '/tools/ground_truth')
try:
    import gt_store
except Exception:
    print('advisory')
    sys.exit(0)
try:
    print(gt_store.enforcement(Path(sys.argv[1])))
except gt_store.MixedLayoutError as exc:
    print(exc)
    sys.exit(3)
except Exception:
    print('advisory')
" "$root" 2>/dev/null); then
  echo "## ground-truth: $mode"
  show_dyn
  exit 0
fi
[ -z "$mode" ] && mode=advisory

cat <<CARDEOF
## ground-truth contract active (enforcement: $mode)
- Understand affected behavior and consumers; inspect the diff and run relevant tests. $name covers only its declared scope.
- Update claims only when their meaning, path or evidence changes. A status raise needs green baseline, red mutation, restored green evidence.
- Skipped or incomplete checks are not proof. Repeated failure: diagnose and report, do not weaken the check.
- Pre-edit gates are opt-in (GT_STRICT_EDIT_GUARD=1). Details: .claude/rules/ground-truth.md
CARDEOF
show_dyn

out=$(cd "$root" && "$py" tools/ground_truth/verify.py --mode=sync --show-warn 2>&1)
rc=$?
if [ $rc -eq 0 ]; then
  warn_out=$(printf '%s\n' "$out" | grep '^\[WARN\]')
  if [ -n "$warn_out" ]; then
    n=$(printf '%s\n' "$warn_out" | wc -l | tr -d ' ')
    echo "## ground-truth: $n warnings in $name (not blocking; inspect relevant warnings)"
    printf '%s\n' "$warn_out" | head -3
    [ "$n" -gt 3 ] && echo "... $((n - 3)) more: $py tools/ground_truth/verify.py --mode=sync --show-warn"
  fi
  exit 0
fi

echo "## ground-truth: $name is currently out of sync"
echo '```'
echo "$out"
echo '```'
exit 0
