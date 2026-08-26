#!/bin/bash
# Предупреждение о разросшейся сессии (правило из CLAUDE.md: ~1500 запросов = пора /clear)
input=$(cat)
tp=$(printf '%s' "$input" | python3 -c "import json,sys;print(json.load(sys.stdin).get('transcript_path',''))" 2>/dev/null)
[ -f "$tp" ] || exit 0
n=$(grep -c '"usage"' "$tp" 2>/dev/null || echo 0)
if [ "$n" -ge 1500 ]; then
  printf '{"hookSpecificOutput":{"hookEventName":"UserPromptSubmit","additionalContext":"SESSION-LENGTH WARNING: %s API-записей в этой сессии. Каждый ход перечитывает весь контекст. Закончена логическая задача - предложи пользователю /clear (handoff в plan.md уже по правилам)."}}\n' "$n"
fi
exit 0
