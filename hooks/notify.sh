#!/bin/bash
# Notification/Stop hook: macOS звук + баннер. Ноль токенов, best-effort.
# Notification — Claude ждёт ввода/разрешения; Stop — ход завершён.
input=$(cat)
event=$(printf '%s' "$input" | jq -r '.hook_event_name // empty')
case "$event" in
  Notification)
    msg=$(printf '%s' "$input" | jq -r '.message // "ждёт ввода"' | tr -d '"\\')
    afplay /System/Library/Sounds/Funk.aiff >/dev/null 2>&1 &
    osascript -e "display notification \"$msg\" with title \"Claude Code\"" >/dev/null 2>&1 || true
    ;;
  Stop)
    afplay /System/Library/Sounds/Glass.aiff >/dev/null 2>&1 &
    ;;
esac
exit 0
