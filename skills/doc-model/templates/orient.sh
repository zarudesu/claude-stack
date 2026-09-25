#!/usr/bin/env bash
# Стартовый пакет doc-model (фаза 7) — хук SessionStart в <root>/.claude/settings.json.
# Derived: вычисляется при старте, ничего не хранит; статус — только канон по routing-таблице L0.
# Копия живёт в <root>/.claude/scripts/orient.sh; переменные в шапке — под канон репозитория.
# Контракт и проверка — references/start-packet.md в скилле. Ручной запуск: bash .claude/scripts/orient.sh
set -uo pipefail
root="${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"
cd "$root" || exit 0
# Маркер старта сессии для Stop-хука write-back (scripts/writeback-check.py в скилле): окно «правки этой сессии».
sid="$(python3 -c 'import json,sys; d=sys.stdin.read(); print(json.loads(d).get("session_id","") if d.strip() else "")' 2>/dev/null)"
gitdir="$(git rev-parse --absolute-git-dir 2>/dev/null)"   # в worktree .git — файл; маркер — в git-dir этого worktree
if [ -n "$sid" ] && [ -n "$gitdir" ]; then
  mkdir -p "$gitdir/doc-model" && : > "$gitdir/doc-model/session-$sid.start"
  find "$gitdir/doc-model" -name 'session-*' -mtime +7 -delete 2>/dev/null
fi

TITLE="Стартовый пакет"                          # заголовок блока в контексте
STATUS_FILE="management/README.md"               # дом статуса/WIP по routing-таблице L0
STATUS_PATTERN="^Текущий учёт WIP"               # строка состояния (grep -m1)
STATUS_COUNTERS='(управление|техника) — (\d+/\d+)'  # python-regex: что из строки показывать; пусто — первые 200 символов
CLAIMS_CMD=".claude/scripts/session-claim.sh list"  # записи key: value, разделённые ---; пусто — git worktree list
ROUTER_FILE=""                                   # derived-роутер задач (L3); пусто — его печатает другой хук
ROUTING_LINE="Перед первым действием — Cold-start в CLAUDE.md: режим → строка направления в каноне → claims. Read-only: claim не нужен; запись/мутация — сначала claim. Движение задачи (claim → план → write-back → release/handoff) — .claude/pm/CLAUDE.md."

echo "## $TITLE (derived, не источник статуса; канон — $STATUS_FILE)"

# 1. Свежесть документации
doclint="$HOME/.claude/skills/doc-model/scripts/doclint.py"
if [ -f "$doclint" ]; then
  python3 "$doclint" --root "$root" --summary --tree 2>/dev/null | sed 's/^/- /'
else
  echo "- doclint: скрипт не найден ($doclint) — свежесть документации не проверена"
fi

# 2. Состояние портфеля + 3. Кто где
{
  grep -m1 "$STATUS_PATTERN" "$STATUS_FILE" 2>/dev/null || true
  echo "=== claims ==="
  if [ -n "$CLAIMS_CMD" ] && [ -x "${CLAIMS_CMD%% *}" ]; then $CLAIMS_CMD 2>/dev/null; fi
} | STATUS_FILE="$STATUS_FILE" STATUS_COUNTERS="$STATUS_COUNTERS" python3 -c '
import os, re, sys
text = sys.stdin.read()
wip, _, claims = text.partition("=== claims ===\n")
wip = wip.strip()
if wip:
    pat = os.environ.get("STATUS_COUNTERS", "")
    found = re.findall(pat, wip) if pat else []
    shown = ", ".join(" ".join(m) if isinstance(m, tuple) else m for m in found) if found else wip[:200]
    print("- Состояние (" + os.environ["STATUS_FILE"] + "): " + shown)
recs = []
for block in claims.split("---\n"):
    rec = dict(l.split(": ", 1) for l in block.splitlines() if ": " in l)
    if rec.get("scope"):
        recs.append(rec)
if recs:
    recs.sort(key=lambda r: r.get("last_touch", ""), reverse=True)
    primary = [r for r in recs if r["scope"].split(":")[0] in ("slug", "mgmt", "research")]
    extra = [r for r in recs if r not in primary]
    print("- Claims — кто где: primary " + str(len(primary)) + ", свежие сверху (полный список: " + "session-claim.sh list" + ")")
    for r in primary[:10]:
        obj = r.get("objective", "")
        obj = obj[:80] + "…" if len(obj) > 80 else obj
        print("  - " + " | ".join([r["scope"], r.get("session", "?"), r.get("mode", "?"), r.get("last_touch", "")[:10], obj]))
    if len(primary) > 10:
        print("  - …ещё " + str(len(primary) - 10))
    if extra:
        print("  - плюс " + str(len(extra)) + " claims file:/external: у сессий: " + ", ".join(sorted({r.get("session", "?") for r in extra})))
'
if [ -z "$CLAIMS_CMD" ] || [ ! -x "${CLAIMS_CMD%% *}" ]; then
  echo "- Кто где (git worktree list):"; git worktree list 2>/dev/null | sed 's/^/  - /'
fi

# 3b. Роутер задач — только если его не инжектит другой хук
[ -n "$ROUTER_FILE" ] && [ -f "$ROUTER_FILE" ] && { echo "- Задачи ($ROUTER_FILE):"; head -n 30 "$ROUTER_FILE" | sed 's/^/  /'; }

# 4. Маршрутизация действий — указатели, не правила
echo "- $ROUTING_LINE"
