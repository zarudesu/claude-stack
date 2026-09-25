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

# stdin is passed straight through to the guard, which reads cwd and
# tool_name out of the hook JSON itself -- consuming it here to find the
# repository would mean re-feeding it to the child.
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || exit 0
root=$(git rev-parse --show-toplevel)
guard="$root/tools/ground_truth/gt_edit_guard.py"
[ -f "$guard" ] || exit 0

# The system python lacks the project's own deps (pyyaml, etc.) -- the
# guard parses STATUS.yaml on a cache miss and would fail on the import.
py="${GT_PYTHON:-}"
[ -z "$py" ] && { [ -x "$root/.venv/bin/python" ] && py="$root/.venv/bin/python" || py="python3"; }

exec "$py" "$guard"
