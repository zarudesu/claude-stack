#!/bin/bash
# Разовая проверка семантического стека: Milvus (контейнер + реальные запросы) и grepai watcher.
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

# --- grepai watcher ---
if ! pgrep -f "grepai watch" >/dev/null 2>&1; then
  fail "grepai watcher не бежит (поднять: cd ~/Projects && grepai watch --background)"
else
  # процесс есть — сверимся с его собственным статусом
  if (cd "$HOME/Projects" && grepai watch --status 2>/dev/null | grep -q "Status: running"); then
    ok "grepai watcher running"
  else
    fail "grepai: процесс есть, но --status не подтверждает running (зомби? pkill -f 'grepai watch' и поднять заново)"
  fi
fi

if [ ${#FAILS[@]} -gt 0 ]; then
  [ "$QUIET" = "--quiet" ] || echo "Семантический стек: ${#FAILS[@]} поломок."
  exit 1
fi
[ "$QUIET" = "--quiet" ] || echo "Семантический стек исправен."
exit 0
