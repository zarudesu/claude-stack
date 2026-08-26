#!/bin/bash
# PreToolUse guard (matcher: Read|Edit|Write). Просит подтверждение на доступ
# к чувствительным файлам (секреты, приватные ключи, vault). НЕ трогает Bash,
# чтобы не ломать рабочий `ssh -i ~/.ssh/id_...` flow.
path=$(jq -r '.tool_input.file_path // empty')
[ -z "$path" ] && exit 0

# исключения (публичные/примеры) — пропускаем без запроса
if echo "$path" | grep -Eq '\.env\.(example|sample|template)$|\.pub$'; then
  exit 0
fi

if echo "$path" | grep -Eq '(^|/)\.env($|\.)|\.pem$|\.key$|(^|/)id_(rsa|ed25519|ecdsa|dsa)([^./]*)?$|(^|/)credentials$|\.aws/credentials|vault\.ya?ml$|\.kdbx$|(^|/)secrets?\.(ya?ml|json|env)$'; then
  jq -n --arg p "$path" '{hookSpecificOutput:{hookEventName:"PreToolUse",permissionDecision:"ask",permissionDecisionReason:("Доступ к чувствительному файлу: " + $p + " — подтверди")}}'
  exit 0
fi
exit 0
