#!/bin/bash
# PreToolUse hook (project-local, committed), matcher Edit|Write|Bash.
# Refuses an edit to a file that sits under the coverage roots while no
# claim covers it -- the cheapest possible reminder, fired at the moment
# the claim is easiest to write.
#
# No tool_name filtering here: this script forwards stdin unchanged for
# every matched tool, Bash included -- the guard itself decides whether a
# Bash command's text carries a write shape and a path worth gating. Two
# of the three checks the guard runs are hard and deny regardless of
# meta.enforcement (the blast gate, the probe budget); only the
# uncovered-file check bends to it. The Stop hook, which reads git state
# and cannot be routed around, stays the backstop for whatever a
# command-text heuristic still misses.
set -u

# Pre-edit ceremony is opt-in. Stop and CI validate the resulting state.
[ "${GT_STRICT_EDIT_GUARD:-0}" = "1" ] || exit 0

# Emergency switch: GT_HOOKS_OFF=1 turns every ground-truth hook off, and
# says so on each run instead of going quiet.
if [ "${GT_HOOKS_OFF:-0}" = "1" ]; then
  echo '{"systemMessage":"ground-truth hooks are off (GT_HOOKS_OFF=1)"}'
  exit 0
fi

# stdin is passed straight through to the guard, which reads cwd and
# tool_name out of the hook JSON itself -- consuming it here to find the
# repository would mean re-feeding it to the child.
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || exit 0
root=$(git rev-parse --show-toplevel)
guard="$root/tools/ground_truth/gt_edit_guard.py"
{ [ -f "$guard" ] && [ -f "$root/STATUS.yaml" ]; } || exit 0

# The system python lacks the project's own deps (pyyaml, etc.) -- the
# guard parses STATUS.yaml on a cache miss and would fail on the import.
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
# Advisory here: the Stop hook blocks on the same broken environment.
# PreToolUse shows stdout JSON on exit 0, not stderr; the message is escaped
# for a JSON string by hand (no python to lean on here).
if ! env_err=$("$py" -c 'import yaml' 2>&1); then
  msg="ground-truth: environment broken: $(printf '%s\n' "$env_err" | tail -1); run: python3 tools/ground_truth/gt_doctor.py"
  printf '{"systemMessage":"%s"}\n' "$(printf '%s' "$msg" | sed 's/\\/\\\\/g; s/"/\\"/g' | tr -d '\000-\037')"
  exit 0
fi

exec "$py" "$guard"
