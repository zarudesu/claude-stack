#!/bin/bash
# Периодический сторож семантического стека (launchd, каждые 10 мин + при загрузке).
# Уведомляет только на СМЕНЕ состояния: работало → сломалось и обратно. Без спама.
set -u
STATE="$HOME/.claude/.semantic-stack-state"
LOG="$HOME/.claude/semantic-stack-check.log"
CHECK="$HOME/.claude/scripts/semantic-stack-check.sh"

note() {
  osascript -e "display notification \"$1\" with title \"Семантический стек (Milvus/grepai)\"" >/dev/null 2>&1 || true
}

OUT=$("$CHECK" --quiet 2>&1)
RC=$?
PREV=$(cat "$STATE" 2>/dev/null || echo unknown)
NOW=$([ $RC -eq 0 ] && echo ok || echo broken)
printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$NOW" >> "$LOG"
[ -n "$OUT" ] && printf '%s\n' "$OUT" >> "$LOG"
# лог не должен расти бесконечно
[ "$(wc -l < "$LOG" 2>/dev/null || echo 0)" -gt 2000 ] && tail -500 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"

if [ "$NOW" != "$PREV" ]; then
  if [ "$NOW" = "broken" ]; then
    note "$(printf '%s' "$OUT" | head -2 | sed 's/^  • //' | tr '\n' ';')"
    afplay /System/Library/Sounds/Funk.aiff >/dev/null 2>&1 &
  elif [ "$PREV" != "unknown" ]; then
    note "восстановлен"
  fi
fi
printf '%s\n' "$NOW" > "$STATE"
exit 0
