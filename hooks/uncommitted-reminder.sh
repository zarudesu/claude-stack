#!/bin/bash
# Stop hook: remind the model to commit when the session's working tree is dirty.
IFS=$'\t' read -r active sid dir < <(jq -r '[.stop_hook_active // false, .session_id // "x", .cwd // ""] | @tsv')
[ "$active" = true ] && exit 0
st=$(git -C "$dir" --no-optional-locks status --porcelain 2>/dev/null) || exit 0
[ -z "$st" ] && exit 0
# same dirty state as the last reminder in this session -> stay quiet
mark="${TMPDIR:-/tmp}/uncommitted-reminder-$sid"
sum=$(printf '%s' "$st" | cksum)
[ "$(cat "$mark" 2>/dev/null)" = "$sum" ] && exit 0
echo "$sum" > "$mark"
n=$(printf '%s\n' "$st" | wc -l | tr -d ' ')
repo=$(git -C "$dir" rev-parse --show-toplevel)
jq -nc --arg m "$n незакоммиченных файлов в $repo — закоммить логический шаг (только свои правки)" \
  '{hookSpecificOutput: {hookEventName: "Stop", additionalContext: $m}}'
