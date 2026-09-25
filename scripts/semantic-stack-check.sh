#!/bin/bash
# Разовая проверка семантического стека: Milvus (контейнер + реальные запросы) и свежесть grepai-индекса.
# exit 0 — всё живо; exit 1 — есть поломки (список в stdout).
# --quiet: печатать только поломки.
set -u
QUIET="${1:-}"
FAILS=()

ok() { [ "$QUIET" = "--quiet" ] || echo "  ✅ $1"; }
fail() { FAILS+=("$1"); echo "  • $1"; }

# --- Milvus ---
if ! docker info >/dev/null 2>&1; then
  fail "docker не отвечает (OrbStack/Docker выключен) — Milvus лежит"
else
  STATE=$(docker inspect -f '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}' milvus-standalone 2>/dev/null)
  if [ -z "$STATE" ]; then
    fail "контейнер milvus-standalone ОТСУТСТВУЕТ (пересоздать: README, раздел «Semantic search стек → Установка»)"
  elif [ "${STATE%% *}" != "running" ]; then
    fail "milvus-standalone не running: $STATE (crash-loop? docker logs milvus-standalone)"
  elif [ "${STATE##* }" != "healthy" ]; then
    fail "milvus-standalone unhealthy: $STATE"
  else
    ok "Milvus контейнер running+healthy"
    # Живой запрос: REST collections/list должен отвечать code:0 и видеть коллекции
    RESP=$(curl -s -m 8 -X POST http://127.0.0.1:19530/v2/vectordb/collections/list \
      -H "Content-Type: application/json" -d '{}' 2>/dev/null)
    if ! printf '%s' "$RESP" | grep -q '"code":0'; then
      fail "Milvus REST не отвечает на запросы (healthz жив, а запросы нет): ${RESP:0:120}"
    elif ! printf '%s' "$RESP" | grep -q 'hybrid_code_chunks'; then
      fail "Milvus пуст — коллекции claude-context пропали (реиндекс: scripts/reindex-claude-context.py)"
    else
      N=$(printf '%s' "$RESP" | grep -o 'hybrid_code_chunks' | wc -l | tr -d ' ')
      ok "Milvus отвечает, коллекций: $N"
    fi
  fi
fi

# --- Ollama (эмбеддер для claude-context) ---
if ! TAGS=$(curl -s -m 5 http://127.0.0.1:11434/api/tags) || [ -z "$TAGS" ]; then
  fail "Ollama не отвечает на 127.0.0.1:11434 — индексация упадёт на detect embedding dimension (поднять: brew services start ollama)"
elif ! printf '%s' "$TAGS" | grep -q 'qwen3-embedding:0.6b'; then
  fail "Ollama жив, но модели qwen3-embedding:0.6b нет (ollama pull qwen3-embedding:0.6b)"
else
  ok "Ollama отвечает, модель qwen3-embedding:0.6b на месте"
fi

# --- grepai index ---
# Постоянного watcher нет намеренно: `watch --background` (0.37) убивает ребёнка, если 2.9 GB gob не загрузился за 30 с,
# а живой watcher держит несколько GB RAM. Индекс освежает ~/.claude/scripts/grepai-resync.sh (launchd, ежедневно 05:30).
GOB="$HOME/Projects/.grepai/index.gob"
if [ ! -s "$GOB" ]; then
  fail "grepai: индекса нет ($GOB) — полная пересборка: ~/.claude/scripts/grepai-resync.sh (~2.5 ч)"
elif pgrep -f "grepai watch" >/dev/null 2>&1; then
  ok "grepai: идёт resync (pid $(pgrep -f 'grepai watch' | head -1))"
else
  age_h=$(( ( $(date +%s) - $(stat -f %m "$GOB") ) / 3600 ))
  if [ "$age_h" -le 48 ]; then
    ok "grepai индекс свежий (${age_h} ч, $(du -h "$GOB" | cut -f1))"
  else
    fail "grepai индекс протух (${age_h} ч) — запустить ~/.claude/scripts/grepai-resync.sh"
  fi
fi

if [ ${#FAILS[@]} -gt 0 ]; then
  [ "$QUIET" = "--quiet" ] || echo "Семантический стек: ${#FAILS[@]} поломок."
  exit 1
fi
[ "$QUIET" = "--quiet" ] || echo "Семантический стек исправен."
exit 0
