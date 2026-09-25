# Write-back и заполнение — контракт механизмов (фаза 8)

## Stop-хук `scripts/writeback-check.py`

Срабатывает после каждого хода ассистента (Stop не фильтруется matcher'ом). Логика:

1. `stop_hook_active: true` или `DOC_MODEL_NO_WRITEBACK=1` или `"writeback": {"enabled": false}` в конфиге → молчит.
2. Маркера `.git/doc-model/session-<session_id>.start` нет → ставит его и молчит (окно правок начинается с этого момента). Маркер пишет стартовый пакет (`orient.sh`) на `startup`/`resume`/`compact`; старше 7 дней удаляются.
3. `git status --porcelain --untracked-files=all`, из списка остаются файлы с mtime новее маркера. Дома заполнения — `.claude/pm/**` плюс `writeback.homes` из конфига (глобы `fnmatch`, например `management/*.md` для репозитория, где management-режим пишет канон, а не план).
4. Есть правки вне домов и ни одной правки в домах → пишет `.git/doc-model/session-<id>.nudged` и возвращает `{"hookSpecificOutput": {"hookEventName": "Stop", "additionalContext": "..."}}`: ход продолжается, модель видит подсказку без hook-error. Иначе молчит. Второй раз за сессию не срабатывает никогда.

Границы: правки соседних окон за то же время выглядят как свои — отсечь их по автору нельзя, поэтому подсказка одна и советует «не твоё — просто заверши». Хук ничего не пишет в рабочее дерево, только маркеры в `.git/doc-model/`.

Хук в `<root>/.claude/settings.json`:
```json
{"hooks": {"Stop": [{"hooks": [{"type": "command",
  "command": "python3 \"$HOME/.claude/skills/doc-model/scripts/writeback-check.py\""}]}]}}
```

Ручная проверка (фикстура): маркер → правка вне pm → подсказка; повтор → тишина; правка плана → тишина; `stop_hook_active` → тишина. Случаи — в `SKILL.md`, фаза 8; на живом репозитории — правка без плана и ход завершён → в транскрипте `Stop hook feedback`.

## Конфиг заполнения (`config.json`)

```json
"pm": {"root": ".claude/pm", "plan": "plan.md", "handoff_glob": "handoff-*.md",
       "containers": ["_management", "_research"],
       "header_lines": 30, "header_keys": ["Next action"], "plan_lines": 600, "handoffs_max": 1},
"rules_dir": ".claude/rules",
"writeback": {"enabled": true, "homes": ["management/*.md"]}
```

Все ключи необязательны, отсутствующие берутся из дефолтов линтера. `header_keys` сверяются без учёта регистра в первых `header_lines` строках плана.

## Что проверяет секция `заполнение`

| Проверка | Цвет | Смысл |
|---|---|---|
| в шапке плана нет `Next action` | FAIL | читающая сессия берёт шапку, не хвост лога |
| план длиннее `plan_lines` | FAIL | старый лог — в `<unit>/archive/`, шапка и текущее состояние остаются |
| handoff-файлов больше `handoffs_max` | WARN | handoff только для незавершённой передачи; законченное — в план |
| `<slug>/` без плана | FAIL | один план на направление; каталог-сирота |
| deliverable в контейнере без плана (с handoff или без) | INFO | management/research-работа, закрытая или в передаче — решает владелец |
| правило `.claude/rules/*.md` без `paths:` | WARN (структура) | грузится всегда — переносить в L0 или дать `paths:` |

Линтер планы не правит и не архивирует: красное в чужом scope — триаж владельцу, в своём — write-back по контракту L1 pm-каталога.
