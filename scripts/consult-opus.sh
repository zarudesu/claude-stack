#!/usr/bin/env bash
# Second opinion от старшей модели для субагентов (sonnet/haiku), которые
# не могут спавнить агентов сами. Headless-вызов claude в print-режиме.
#
# Использование:
#   consult-opus.sh "вопрос одним абзацем" [file1 file2 ...]
#   echo "контекст" | consult-opus.sh "вопрос"
#
# Модель по умолчанию — opus; override: CONSULT_MODEL=sonnet|opus|fable.
# Консультант read-only (Read/Grep/Glob), максимум 6 turns.
set -euo pipefail

MODEL="${CONSULT_MODEL:-opus}"
QUESTION="${1:?usage: consult-opus.sh \"вопрос\" [files...]}"
shift || true

CTX=""
if [ ! -t 0 ]; then CTX="$(cat || true)"; fi
for f in "$@"; do
  [ -f "$f" ] || { echo "consult-opus: файл не найден: $f" >&2; exit 1; }
  CTX="${CTX}

--- ${f} ---
$(cat "$f")"
done

PROMPT="Ты — консультант, дающий second opinion другому инженеру. Ответь кратко и по существу: 1) вердикт/рекомендация одной строкой, 2) обоснование в 2-5 предложениях, 3) на что обратить внимание. Без воды и дисклеймеров. Если вопроса недостаточно для уверенного ответа — скажи, каких данных не хватает.

Вопрос: ${QUESTION}${CTX:+

Контекст:
${CTX}}"

printf '%s' "$PROMPT" | claude -p --model "$MODEL" --max-turns 6 --allowedTools "Read,Grep,Glob"
