#!/bin/bash
# or-task.sh — делегирование атомарной задачи дешёвой модели через OpenRouter.
# Usage: or-task.sh [-m flash|pro|qwen|<model-id>] [-o max_tokens] [-t timeout_s] "бриф" [файл ...]
#   flash = deepseek/deepseek-v4-flash-0731   (дефолт: рутина, саммари, черновики)
#   pro   = deepseek/deepseek-v4-pro-0813     (тяжёлые черновики/планы)
#   qwen  = qwen/qwen3-coder-30b-a3b-instruct (кодовые правки по точному брифу)
# Ключ: ~/.config/openrouter/key.env (OPENROUTER_API_KEY). Ответ -> stdout, usage/цена -> stderr.
set -euo pipefail

ENV_FILE="$HOME/.config/openrouter/key.env"
[ -f "$ENV_FILE" ] || { echo "ERROR: нет $ENV_FILE (ключ OpenRouter)" >&2; exit 1; }
# shellcheck disable=SC1090
source "$ENV_FILE"
[ -n "${OPENROUTER_API_KEY:-}" ] || { echo "ERROR: OPENROUTER_API_KEY пуст в $ENV_FILE" >&2; exit 1; }

MODEL="deepseek/deepseek-v4-flash-0731"
MAX_TOKENS=4096          # cap на выход — фоллбэк-правило 3
TIMEOUT=180              # секунд на весь вызов
MAX_INPUT_CHARS=300000   # ~75k токенов; больше — дели задачу, не заливай репу

while getopts "m:o:t:" opt; do
  case $opt in
    m) case "$OPTARG" in
         flash) MODEL="deepseek/deepseek-v4-flash-0731" ;;
         pro)   MODEL="deepseek/deepseek-v4-pro-0813" ;;
         qwen)  MODEL="qwen/qwen3-coder-30b-a3b-instruct" ;;
         *)     MODEL="$OPTARG" ;;
       esac ;;
    o) MAX_TOKENS="$OPTARG" ;;
    t) TIMEOUT="$OPTARG" ;;
    *) exit 64 ;;
  esac
done
shift $((OPTIND-1))

[ $# -ge 1 ] || { echo "usage: or-task.sh [-m flash|pro|qwen] [-o max_tokens] [-t timeout] \"бриф\" [файл...]" >&2; exit 64; }
BRIEF="$1"; shift

USER_CONTENT="$BRIEF"
for f in "$@"; do
  [ -f "$f" ] || { echo "ERROR: файл не найден: $f" >&2; exit 1; }
  USER_CONTENT+=$'\n\n'"=== FILE: $f ==="$'\n'"$(cat "$f")"
done
if [ ${#USER_CONTENT} -gt "$MAX_INPUT_CHARS" ]; then
  echo "ERROR: ввод ${#USER_CONTENT} симв. > cap $MAX_INPUT_CHARS — раздели задачу" >&2; exit 1
fi

SYSTEM='Ты — исполнитель одной атомарной задачи. Правила: выполняй строго бриф, ничего сверх него; если бриф неоднозначен — явно скажи, в чём неоднозначность, и не угадывай; к результату приложи evidence: что именно сделал и на что во входных данных опирался; отвечай на языке брифа.'

export OR_MODEL="$MODEL" OR_MAX_TOKENS="$MAX_TOKENS" OR_SYSTEM="$SYSTEM" OR_USER="$USER_CONTENT"
PAYLOAD=$(python3 - <<'PY'
import json, os
print(json.dumps({
    "model": os.environ["OR_MODEL"],
    "max_tokens": int(os.environ["OR_MAX_TOKENS"]),
    "messages": [
        {"role": "system", "content": os.environ["OR_SYSTEM"]},
        {"role": "user", "content": os.environ["OR_USER"]},
    ],
    "usage": {"include": True},
}))
PY
)

RESP=$(curl -sS --max-time "$TIMEOUT" https://openrouter.ai/api/v1/chat/completions \
  -H "Authorization: Bearer $OPENROUTER_API_KEY" \
  -H "Content-Type: application/json" \
  --data-binary "$PAYLOAD") || { echo "ERROR: curl fail (сеть/таймаут ${TIMEOUT}s) — 1 ретрай, потом sonnet с пометкой" >&2; exit 2; }

export OR_RESP="$RESP"
python3 - <<'PY'
import json, os, sys
try:
    d = json.loads(os.environ["OR_RESP"])
except json.JSONDecodeError:
    print("ERROR: не-JSON ответ API:", os.environ["OR_RESP"][:500], file=sys.stderr); sys.exit(3)
if "error" in d:
    e = d["error"]; code = e.get("code"); msg = e.get("message", "")
    hint = {401: " — ключ невалиден/протух (~/.config/openrouter/key.env); явно сообщить юзеру, работать на подписке",
            402: " — баланс OpenRouter кончился (openrouter.ai/credits); явно сообщить юзеру, работать на подписке",
            429: " — rate limit, повторить позже"}.get(code, "")
    print(f"ERROR {code}: {msg}{hint}", file=sys.stderr); sys.exit(3)
print(d["choices"][0]["message"]["content"])
u = d.get("usage", {})
print(f"--- model={d.get('model')} in={u.get('prompt_tokens')} out={u.get('completion_tokens')} cost=${u.get('cost')}", file=sys.stderr)
PY
