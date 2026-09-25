Показать отчёт «работа/ожидание» по Claude Code и Codex.

Запусти `python3 ~/.claude/scripts/time-report.py $ARGUMENTS` и выведи результат как есть (в code block), без пересказа.

Флаги (передаются как аргументы команды): `--days N` (период, default 7), `--sessions` (детализация по сессиям с первым промптом), `--app claude|codex` (фильтр).

Источники: Claude Code — `~/.claude/time-tracking.jsonl` (пишут хуки `~/.claude/hooks/track-time.sh` из settings.json); Codex — его собственные rollout-логи `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl` (хуки Codex не используются — он запускает только доверенные, доверие выдаётся интерактивно).

Это локальный учёт времени. Стоимость, токены, латентность и лента промптов — в Grafana по OTel-телеметрии: `$OTEL_GRAFANA_URL/d/ai-telemetry` (например, `https://grafana.example.com/d/ai-telemetry`), если этот стек поднят.
