#!/bin/bash
# PreToolUse (Bash): перед `git commit` сканит staged-изменения gitleaks'ом.
# Блокирует (deny) если в staged найден секрет — content-based, ловит токен
# ВНУТРИ разрешённого пути-гардом файла. Тихо пропускает вне git/без gitleaks.
input=$(cat)
cmd=$(printf '%s' "$input" | jq -r '.tool_input.command // empty')
[ -z "$cmd" ] && exit 0
printf '%s' "$cmd" | grep -Eq 'git[[:space:]]+commit' || exit 0
command -v gitleaks >/dev/null 2>&1 || exit 0
cwd=$(printf '%s' "$input" | jq -r '.cwd // empty')
[ -n "$cwd" ] && cd "$cwd" 2>/dev/null
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || exit 0

out=$(gitleaks git --staged --no-banner 2>&1)
if [ $? -ne 0 ]; then
  rep=$(printf '%s' "$out" | grep -iE 'finding|secret|rule|file:' | head -c 600)
  jq -n --arg r "gitleaks обнаружил секрет в staged — commit заблокирован. $rep" \
    '{hookSpecificOutput:{hookEventName:"PreToolUse",permissionDecision:"deny",permissionDecisionReason:$r}}'
fi
exit 0
