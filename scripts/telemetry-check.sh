#!/bin/bash
# Проверка контура телеметрии агентов (self-hosted OTLP-приёмник + Grafana) — от конфигов на маке до приёма на сервере.
# Креды берутся из самих конфигов, Vaultwarden не нужен.
#
#   telemetry-check.sh          отчёт построчно, exit 1 при поломке
#   telemetry-check.sh --quiet  молча; печатает только проблемы (для launchd/cron)
#
# Проверяет ровно то, что ломалось на практике: пропажу env, delta-темпоральность,
# неполные per-signal endpoint'ы у Codex, недоступность/закрытость ingest.
set -u
QUIET=0
[ "${1:-}" = "--quiet" ] && QUIET=1
SETTINGS="$HOME/.claude/settings.json"
CODEX="$HOME/.codex/config.toml"
PROBLEMS=()

ok()   { [ "$QUIET" = 1 ] || printf '  ✅ %s\n' "$1"; }
bad()  { PROBLEMS+=("$1"); [ "$QUIET" = 1 ] || printf '  ❌ %s\n' "$1"; }
head_() { [ "$QUIET" = 1 ] || printf '%s\n' "$1"; }

head_ "── конфиг Claude Code ──"
if [ -r "$SETTINGS" ] && jq -e . "$SETTINGS" >/dev/null 2>&1; then
  ENDPOINT=$(jq -r '.env.OTEL_EXPORTER_OTLP_ENDPOINT // empty' "$SETTINGS")
  AUTH=$(jq -r '.env.OTEL_EXPORTER_OTLP_HEADERS // empty' "$SETTINGS")
  [ "$(jq -r '.env.CLAUDE_CODE_ENABLE_TELEMETRY // empty' "$SETTINGS")" = "1" ] \
    && ok "телеметрия включена" || bad "CLAUDE_CODE_ENABLE_TELEMETRY не выставлен"
  [ -n "$ENDPOINT" ] && ok "endpoint $ENDPOINT" || bad "OTEL_EXPORTER_OTLP_ENDPOINT пуст"
  [ -n "$AUTH" ] && ok "заголовок авторизации на месте" || bad "OTEL_EXPORTER_OTLP_HEADERS пуст"
  # без cumulative Prometheus молча выбрасывает метрики
  [ "$(jq -r '.env.OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE // empty' "$SETTINGS")" = "cumulative" ] \
    && ok "темпоральность cumulative" || bad "нет cumulative — метрики Claude будут молча теряться"
else
  bad "settings.json нечитаем или битый JSON"
  ENDPOINT="" AUTH=""
fi

head_ "── конфиг Codex ──"
if [ -r "$CODEX" ]; then
  MISSING=""
  HOST="${ENDPOINT#*://}"   # хост берём из endpoint'а Claude Code: у Codex он тот же
  for sig in logs metrics traces; do
    grep -q "${HOST:-<endpoint>}/v1/$sig" "$CODEX" || MISSING="$MISSING $sig"
  done
  [ -z "$MISSING" ] && ok "endpoint'ы logs/metrics/traces заданы полными путями" \
                    || bad "у Codex неполные endpoint'ы:$MISSING (Rust-экспортер путь не дописывает)"
else
  bad "$CODEX не найден"
fi

head_ "── приём на сервере ──"
if [ -n "${ENDPOINT:-}" ]; then
  C_NOAUTH=$(curl -s -m 15 -o /dev/null -w '%{http_code}' -X POST -H 'Content-Type: application/json' \
                  -d '{}' "$ENDPOINT/v1/metrics" 2>/dev/null)
  case "$C_NOAUTH" in
    401) ok "ingest закрыт без авторизации" ;;
    000) bad "ingest недоступен (нет сети или сервер лежит)" ;;
    200) bad "ingest ОТКРЫТ без авторизации — принимает чужие данные" ;;
    *)   bad "ingest без авторизации ответил $C_NOAUTH (ожидался 401)" ;;
  esac
  if [ -n "${AUTH:-}" ] && [ "$C_NOAUTH" != "000" ]; then
    C_AUTH=$(curl -s -m 15 -o /dev/null -w '%{http_code}' -X POST -H "${AUTH/=/: }" \
                  -H 'Content-Type: application/json' -d '{}' "$ENDPOINT/v1/metrics" 2>/dev/null)
    [ "$C_AUTH" = "200" ] && ok "ingest принимает с нашими кредами" \
                          || bad "ingest с нашими кредами ответил $C_AUTH (ожидался 200)"
  fi
  C_GRAF=$(curl -s -m 15 "$ENDPOINT/api/health" 2>/dev/null | jq -r '.database // empty')
  [ "$C_GRAF" = "ok" ] && ok "Grafana жива" || bad "Grafana не отвечает healthcheck'ом"
fi

if [ ${#PROBLEMS[@]} -eq 0 ]; then
  [ "$QUIET" = 1 ] || printf '\nКонтур телеметрии исправен.\n'
  exit 0
fi
printf 'Телеметрия сломана (%d):\n' "${#PROBLEMS[@]}"
printf '  • %s\n' "${PROBLEMS[@]}"
exit 1
