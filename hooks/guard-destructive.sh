#!/bin/bash
# PreToolUse guard (matcher: Bash). Просит подтверждение на потенциально
# деструктивные команды. Bash(*) у пользователя в allow → иначе всё выполняется
# без запроса; этот guard возвращает "ask" для опасных паттернов.
cmd=$(jq -r '.tool_input.command // empty')
[ -z "$cmd" ] && exit 0

if echo "$cmd" | grep -Eq 'rm[[:space:]]+-[a-zA-Z]*[rf]|(^|[[:space:]])mkfs|(^|[[:space:]])dd[[:space:]]+if=|>[[:space:]]*/dev/(sd|disk|nvme|rdisk)|chmod[[:space:]]+-R[[:space:]]+777|terraform[[:space:]]+(apply|destroy)|kubectl[[:space:]]+delete|git[[:space:]]+push[[:space:]]+.*(--force|-f([[:space:]]|$))|git[[:space:]]+reset[[:space:]]+--hard|DROP[[:space:]]+(TABLE|DATABASE|SCHEMA)|TRUNCATE[[:space:]]+TABLE'; then
  jq -n '{hookSpecificOutput:{hookEventName:"PreToolUse",permissionDecision:"ask",permissionDecisionReason:"Потенциально деструктивная команда — подтверди выполнение"}}'
  exit 0
fi
exit 0
