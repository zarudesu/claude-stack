#!/bin/bash
# Stop hook (project-local, committed -- travels with the repo to every
# clone, teammate checkout, and CI runner). Gates on git status plus the
# verifier's own exit code, never on which tool produced the change: a
# tool-name matcher is exactly the hole a disabled repo-wide gate slipped
# real edits through in this editor's history, so this hook only looks at
# observable state (git status, verifier exit code), which is invariant
# regardless of how the file was changed.
set -u

input=$(cat)

# jq is the normal path for reading the hook JSON; if it is missing from
# PATH, fall back to python3's stdlib json rather than let every read
# below silently degrade to "" (which would key the red counter on one
# shared "" session and break the stop_hook_active loop guard too).
have_jq=1
command -v jq >/dev/null 2>&1 || have_jq=0

# Loop guard: if this hook already fired once for this stop event and the
# session is being asked to stop again, do not fire a second time.
if [ "$have_jq" = 1 ]; then
  stop_hook_active=$(printf '%s' "$input" | jq -r '.stop_hook_active // false')
else
  stop_hook_active=$(printf '%s' "$input" | python3 -c '
import json, sys
try:
    v = json.load(sys.stdin).get("stop_hook_active", False)
except Exception:
    v = False
print("true" if v else "false")
')
fi
[ "$stop_hook_active" = "true" ] && exit 0

if [ "$have_jq" = 1 ]; then
  cwd=$(printf '%s' "$input" | jq -r '.cwd // empty')
  sid=$(printf '%s' "$input" | jq -r 'if (.session_id // "") == "" then "unknown" else .session_id end')
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

# A session that left the tree clean has nothing to hand off: it is never
# blocked here. Committed or sibling-repo drift is still reported by the
# SessionStart memory status and failed by the CI memory check.
git -C "$root" status --porcelain 2>/dev/null | grep -q . || exit 0
# Memory runs before the STATUS shortcut: a memory-only repo has no STATUS.yaml.
# No helper installed (no .ground-truth/model.yaml at install time) -> skipped.
memory="$root/tools/ground_truth/gt_context.py"
if [ -f "$memory" ]; then
  "$py" "$memory" hook --event stop
  memory_rc=$?
  [ "$memory_rc" -ne 0 ] && exit 2
fi
[ -f "$status_file" ] || exit 0
verifier="$root/tools/ground_truth/verify.py"
[ -f "$verifier" ] || exit 0

# Read the current enforcement policy before running checks.
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

out=$(cd "$root" && "$py" "$verifier" --mode=sync 2>&1)
rc=$?

# The session guard proves what the verifier cannot see from shape alone:
# that touched claim checks are green and new/raised claims describe a mutation.
# Mutation execution is deferred to isolated CI; R3 is only advisory. Its
# stderr is left alone so an internal warning reaches the transcript even
# when everything else is green.
guard="$root/tools/ground_truth/gt_session_guard.py"
guard_out=""
guard_rc=0
if [ -f "$guard" ]; then
  guard_out=$(cd "$root" && "$py" "$guard" --mode hook)
  guard_rc=$?
fi

# Line references drift silently as unrelated edits shift the file: caught
# here, not left for the next person to notice the citation points at the
# wrong line. A tool error (rc=2) is a problem with the check itself, not
# with STATUS.yaml, so it is only ever a warning, never red.
remapper="$root/tools/ground_truth/remap_line_refs.py"
remap_out=""
remap_rc=0
if [ -f "$remapper" ]; then
  remap_out=$(cd "$root" && "$py" "$remapper" --check --base HEAD STATUS.yaml 2>&1)
  remap_rc=$?
  if [ $remap_rc -eq 2 ]; then
    echo "ground-truth: remap_line_refs.py --check failed, skipping: $remap_out" >&2
    remap_rc=0
  fi
fi

state="$root/tools/ground_truth/gt_hook_state.py"

if [ $rc -eq 0 ] && [ $guard_rc -eq 0 ]; then
  # A bounded/incomplete hook run is not a fully verified result.
  printf '%s\n' "$guard_out" | grep '^WARN ' | head -3 >&2
  [ "$mode" = "blocking" ] && [ -f "$state" ] && "$py" "$state" --root "$root" stop-green "$sid" >/dev/null 2>&1
  if [ $remap_rc -eq 1 ]; then
    echo "ground-truth: [WARN] line references drifted" >&2
    echo "$remap_out" >&2
  fi
  exit 0
fi

escalate=""
if [ "$mode" = "blocking" ] && [ -f "$state" ]; then
  n=$("$py" "$state" --root "$root" stop-red "$sid" 2>/dev/null)
  if [ -n "$n" ] && [ "$n" -ge 3 ] 2>/dev/null; then
    escalate="ESCALATE: STATUS.yaml still red after $n sync rounds -- stop patching, report the red checks below to the owner"
  fi
fi

if [ "$mode" = "blocking" ]; then
  [ -n "$escalate" ] && echo "$escalate" >&2
  echo "ground-truth: STATUS.yaml out of sync -- fix before stopping:" >&2
  [ $rc -ne 0 ] && echo "$out" >&2
  [ $guard_rc -ne 0 ] && echo "$guard_out" >&2
  [ $remap_rc -eq 1 ] && echo "$remap_out" >&2
  exit 2
else
  echo "ground-truth (advisory): STATUS.yaml drift detected, not blocking yet:" >&2
  [ $rc -ne 0 ] && echo "$out" >&2
  [ $guard_rc -ne 0 ] && echo "$guard_out" >&2
  [ $remap_rc -eq 1 ] && echo "$remap_out" >&2
  exit 0
fi
