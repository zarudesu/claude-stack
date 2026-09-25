#!/bin/bash
# Периодическая проверка контура телеметрии (launchd, каждые 30 мин + при загрузке).
# Уведомляет только на СМЕНЕ состояния: работало → сломалось и обратно. Без спама.
# Ноутбук без интернета (самолёт, метро) — не поломка: молча выходим, состояние не трогаем.
set -u
STATE="$HOME/.claude/.telemetry-state"
LOG="$HOME/.claude/telemetry-check.log"
CHECK="$HOME/.claude/scripts/telemetry-check.sh"

note() {
  osascript -e "display notification \"$1\" with title \"Телеметрия агентов\"" >/dev/null 2>&1 || true
}

# Нет интернета — не наша поломка.
curl -s -m 8 -o /dev/null https://cloudflare.com/cdn-cgi/trace 2>/dev/null || exit 0

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
    note "$(printf '%s' "$OUT" | tail -n +2 | head -2 | sed 's/^  • //' | tr '\n' ';')"
    afplay /System/Library/Sounds/Funk.aiff >/dev/null 2>&1 &
  elif [ "$PREV" != "unknown" ]; then
    note "восстановлена"
  fi
fi
printf '%s\n' "$NOW" > "$STATE"
exit 0
