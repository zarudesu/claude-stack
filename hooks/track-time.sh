#!/bin/bash
# Лог «работа/ожидание» для Claude Code и Codex → ~/.claude/time-tracking.jsonl
# Вызов: track-time.sh <app> [event-fallback]
#   Claude Code (settings.json): track-time.sh claude          (event из hook_event_name)
#   Codex (config.toml hooks):   track-time.sh codex <Event>   (event аргументом, payload может отличаться)
# Никогда не блокирует сессию: любые ошибки → exit 0, stdout пустой
# (stdout SessionStart/UserPromptSubmit-хуков инжектится в контекст).
APP="${1:-claude}"
EVT_ARG="${2:-}"
LOG="$HOME/.claude/time-tracking.jsonl"

command -v jq >/dev/null 2>&1 || exit 0
input=$(cat)
[ -z "$input" ] && exit 0

printf '%s' "$input" | jq -c \
  --arg ts "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg app "$APP" \
  --arg evt "$EVT_ARG" \
  '{ts: $ts, app: $app,
    event: (.hook_event_name // (if $evt == "" then "?" else $evt end)),
    session: (.session_id // .thread_id // .transcript_path // null),
    cwd: (.cwd // .workspace.current_dir // null),
    source: (.source // null),
    prompt: (if .prompt then (.prompt | tostring | .[0:200]) else null end)}
   | with_entries(select(.value != null))' \
  >> "$LOG" 2>/dev/null

exit 0
