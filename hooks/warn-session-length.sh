#!/bin/bash
# Длина сессии (UserPromptSubmit). Правило из CLAUDE.md: ~1500 запросов = пора /clear.
#   n >= WARN  -> предупреждение в контекст (как раньше)
#   n >= HARD  -> промпт блокируется (exit 2), пока не сделан /clear
# Обход для человека: промпт начинается с "override:" — поднимает потолок на +STEP,
# максимум MAX_OVERRIDES раз за сессию (счётчик в state-файле по session_id).
# Слэш-команды (/clear, /compact, /login ...) пропускаются всегда.
WARN=1500; HARD=2500; STEP=500; MAX_OVERRIDES=2
STATE_DIR="$HOME/.claude/state/session-overrides"

input=$(cat)
read -r tp sid prompt < <(printf '%s' "$input" | python3 -c '
import json,sys
d=json.load(sys.stdin)
p=(d.get("prompt") or "").strip().replace("\n"," ")[:40]
print(d.get("transcript_path",""), d.get("session_id","-"), p)' 2>/dev/null)
[ -f "$tp" ] || exit 0
n=$(grep -c '"usage"' "$tp" 2>/dev/null || echo 0)
[ "$n" -ge "$WARN" ] || exit 0

case "$prompt" in /*) exit 0 ;; esac   # слэш-команды всегда проходят

mkdir -p "$STATE_DIR"
sf="$STATE_DIR/$sid"
used=$(cat "$sf" 2>/dev/null || echo 0)

if printf '%s' "$prompt" | grep -qi '^override:'; then
  if [ "$used" -lt "$MAX_OVERRIDES" ]; then
    used=$((used+1)); echo "$used" > "$sf"
  fi
fi
cap=$((HARD + STEP*used))

if [ "$n" -ge "$cap" ]; then
  if [ "$used" -lt "$MAX_OVERRIDES" ]; then
    echo "SESSION-LENGTH HARD CAP: $n API-записей (потолок $cap). Сделай /clear (handoff уже в plan.md). Разово продолжить: промпт с префиксом 'override:' (+$STEP, осталось $((MAX_OVERRIDES-used)) из $MAX_OVERRIDES)." >&2
  else
    echo "SESSION-LENGTH HARD CAP: $n API-записей, override исчерпан ($MAX_OVERRIDES/$MAX_OVERRIDES). Только /clear." >&2
  fi
  exit 2
fi

printf '{"hookSpecificOutput":{"hookEventName":"UserPromptSubmit","additionalContext":"SESSION-LENGTH WARNING: %s API-записей в этой сессии (жёсткий потолок %s, дальше промпты блокируются). Каждый ход перечитывает весь контекст. Закончена логическая задача - предложи пользователю /clear (handoff в plan.md уже по правилам). Новых волн агентов не запускать."}}\n' "$n" "$cap"
exit 0
