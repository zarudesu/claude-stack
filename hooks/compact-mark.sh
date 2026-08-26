#!/bin/bash
# PreCompact: self-replacing marker in the project's _active.md so that after
# compaction the main thread knows context was cut and re-reads plan.md.
# Keeps exactly ONE marker line: old markers (legacy spam included) are removed first.
input=$(cat)
cwd=$(printf '%s' "$input" | jq -r '.cwd // empty')
sid=$(printf '%s' "$input" | jq -r '.session_id // empty' | cut -c1-8)
trig=$(printf '%s' "$input" | jq -r '.trigger // "auto"')
f="$cwd/.claude/pm/_active.md"
[ -n "$cwd" ] && [ -f "$f" ] || exit 0
tmp=$(mktemp "${f}.XXXXXX") || exit 0
awk '
  /^> ⚠ compact / { next }
  /^> последний compact:/ { next }
  { lines[++n] = $0 }
  END {
    while (n > 0 && lines[n] ~ /^[[:space:]]*$/) n--
    prevblank = 0
    for (i = 1; i <= n; i++) {
      if (lines[i] ~ /^[[:space:]]*$/) { if (prevblank) continue; prevblank = 1 } else prevblank = 0
      print lines[i]
    }
  }' "$f" > "$tmp" || { rm -f "$tmp"; exit 0; }
printf '\n> последний compact: %s (%s, session %s) — контекст обрезан: перечитай plan.md активной задачи, не полагайся на память.\n' \
  "$(date '+%Y-%m-%d %H:%M')" "$trig" "$sid" >> "$tmp"
mv "$tmp" "$f"
exit 0
