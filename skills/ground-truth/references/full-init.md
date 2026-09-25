> Исторический справочник механизмов, НЕ второй SKILL.md. Современный init всегда тщательно готовит согласованный проект по init.md и project-memory.md; изложенный здесь STATUS-конвейер сам по себе его не завершает. Прежние модельные разрешения, лимиты чтений, роли, человеческая документация и постоянные флоты не действуют. Цель, полномочия и приёмка определяются текущим ../SKILL.md. Не читать целиком для обычной задачи.

---
name: ground-truth
description: >-
  Приводит git-репозиторий в проверяемое состояние: машиночитаемый контракт
  STATUS.yaml (компонент → implemented/partial/stub/absent/design-only,
  доказано тестом или пробой), тест/CI-джоб, падающий при расхождении кода и
  контракта, структурный долг с id, AI-first навигация «хочу поменять X»,
  Stop/SessionStart-хуки как enforcement. Вызывать, когда просят «привести
  репо к проверяемому состоянию», завести «контракт состояния» или
  STATUS.yaml, когда «доки врут про код», когда есть «заглушки под видом
  реализации», или явно просят режим ground-truth init/audit/sync. Вызов
  скилла на конкретный прогон — standing permission на модель fable в ролях
  judge/synthesis/fleet-probe-grader внутри этого прогона прямым вызовом
  Agent tool; в Workflow-скриптах эти роли идут на opus (D34, см. §2);
  decider — сессионная модель main, mapping-audit — opus; отдельный агент
  fable-deep сюда не входит.
disable-model-invocation: true
---

# ground-truth — контракт состояния + верификатор + AI-навигация

Исходная спецификация и журнал решений (D1, D2, … — номера в тексте ссылаются на него) ведутся в приватных заметках автора и не опубликованы. Этот файл — операционный поток; за деталями схемы/верификатора/хуков/промптов — в `references/` (§8).

## 0. Диспетчер режима

1. `git rev-parse --show-toplevel`. Нет `.git` в cwd, но внутри найдены вложенные `.git` → это умбрелла: перечислить найденные репо и спросить, с каким работать; `STATUS.yaml` на умбрелле не создавать никогда (§3).
1a. В выбранном репо есть `<root>/.claude/pm/ground-truth-<repo>/plan.md` (или строка `ground-truth-<repo>` в `<root>/.claude/pm/_active.md`) → это незавершённый прогон: прочитать план, продолжить с первого незакрытого шага; фазы с готовыми артефактами в `research/` (отчёты I.1, `i2-i35-*-final.json`) заново не запускать. Пункт 2 в этом случае не применяется.
2. В выбранном репо нет `STATUS.yaml` → `init` (§4.1). `STATUS.yaml` есть → по умолчанию `audit` (§4.2).
3. `sync` (§4.3) — только по явному аргументу `sync` или из контекста Stop-хука (хук печатает вывод верификатора, сессия отрабатывает sync-дельту).
4. Явный аргумент `init|audit|sync` перекрывает пункты 2–3.

## 1. Цель и что считается VERIFIED

Репозиторий VERIFIED, когда: (1) один `STATUS.yaml` на git-репо описывает каждый компонент в `meta.coverage.roots` статусом, доказанным `check`'ом (тест/проба), не намерением; (2) тест/CI падает в момент, когда код и контракт расходятся; (3) доки сверены с кодом (код — источник правды всегда), человеческая проза — либо реорганизована, либо в карантине `docs/_human/`; (4) долг оформлен структурно (`debt:` с `ref` на claim, не размазан по TODO/plan.md); (5) свежий агент отвечает «хочу поменять X → id, путь, blast radius, статус» за ≤3 чтения; (6) протокол сессии enforced хуком+CI, не только текстом.

Не цель: полнота ради полноты (A1 — 10 проверяемых лучше 50 непроверяемых), имплементация найденных заглушек (D12 — решение владельца, не факт о коде), нарративная документация для людей как самоцель, агрегация статуса поверх нескольких git-репо зонтичного воркспейса (один прогон = один git-репо, §3 ниже).

| Слой | Владеет | Пишет | Использование ground-truth |
|---|---|---|---|
| `STATUS.yaml` | истина о компоненте | init/audit/sync-конвейер | это и есть предмет скилла |
| `doc-model` | навигация по человеко-читаемому знанию | существующий скилл | опциональная фаза ПОСЛЕ установления правды (§4, Phase I.7) |
| `pm` (plan.md/_active) | состояние задачи прогона | pm по ходу работы | бухгалтерия прогона; статус компонента сюда не пишется никогда |
| auto-memory | факты, переживающие задачу и репо | main, когда факт переживёт контракт | «в этом репо контракт с такой-то даты», не состояние компонентов |

## 2. Приоритет и полномочия

**D10 — скилл верхнего приоритета.** На время прогона `ground-truth` вправе переопределять прежние хуки/правила репозитория там, где они противоречат протоколу §5 (например: правило «не трогай `.claude/hooks/`» уступает, если это committed project-local хук самого ground-truth). Итоговое состояние (`STATUS.yaml` + навигационный слой) — закон для последующих сессий, не рекомендация.

**Модельная таблица (spec §6, амендed D26 — «без экономии», owner поднял модели финдеров/reconcile/adversarial):**

| Роль | Модель | Effort | Обязательность |
|---|---|---|---|
| Finder — inventory | opus (D26: haiku соврал про «нет тестов») | medium | всегда |
| Finder — claims / code map / debt-scanner | opus (D26, поднято с sonnet) | default | всегда, параллельно |
| Reconcile-per-component | opus (D26) | default | черновик, не финальное суждение |
| Adversarial verify (prosecutor+defender) | opus (D34) | high | асимметрично: 3-vote при повышении confidence или code-fix (D12), 1 проход при понижении |
| Judge/synthesis | opus (D34) | xhigh | обязательно на каждый батч 5–8 |
| Mapping-audit reviewer | opus, независимый от drafting-агента | high | обязательно |
| Code-fix dev | opus, TDD red→green | medium | Phase I.4, по утверждённому брифу |
| Doc-fix editor | opus, один документ на агента | medium | Phase I.5 |
| Quarantine-classifier | opus | medium | Phase I.5 |
| Verifier/hook/CI автор | opus | medium | чистая механика |
| Fleet-probe навигаторы (5×) | opus, свежие, без истории прогона | medium | тестируют навигацию |
| Fleet-probe grader | opus (D34) | high | обязательно — не должен делить слепые пятна навигатора |
| Mutation-selfcheck | детерминированный скрипт, main вызывает напрямую | — | без модели |
| Main (decider) | сессионная модель | high | сквозь весь прогон: ревью, owner-calls, коммит |

**Модель судейских ролей задаётся явно (D34).** В Workflow-скриптах строка `model: 'fable'` не резолвится, а пропущенный `model` молча уходит в дефолт субагентов (`CLAUDE_CODE_SUBAGENT_MODEL`, с 2026-09-23 = opus) без явного effort — поэтому adversarial prosecutor/defender, judge/synthesis и fleet-probe grader внутри workflow идут на `opus` с effort из таблицы. `fable` в этих ролях допустим только прямым вызовом Agent tool (standing permission ниже), не в Workflow.

**Standing permission (D5/D10, сужено D34).** Вызов `ground-truth` на конкретный прогон сам по себе — разрешение на fable в ролях judge/synthesis/fleet-probe-grader ВНУТРИ этого прогона, без запроса на каждый вызов и только прямым вызовом Agent tool с `model: fable` (decider остаётся сессионной моделью main, mapping-audit — opus, см. таблицу выше — обе роли НЕ входят в fable-permission). **Это НЕ распространяется на `fable-deep`** (`claude-fable-5[1m]`+`effort:xhigh`, глобальный тяжёлый агент) — там правило «спроси одной строкой и жди явного „да“» продолжает действовать как обычно; это два разных правила (модель роли внутри прогона vs отдельный поименованный тяжёлый агент), не противоречащих друг другу.

## 3. Инварианты

- **Код — источник правды, всегда (D12).** Конфликт док↔код → правится док (99%). Код правится только там, где он разошёлся сам с собой: красный тест, мёртвая проводка, отсутствующий тест/проба под уже заявленный `check`. Заглушка под видом реализации → в контракте честно `stub`, имплементация — отдельное решение владельца в Outcome, никогда не auto-fix.
- **Повышение статуса без правки check'а ловится детерминированно (D32).** `check_status_raise` сравнивает claim'ы с базовым `STATUS.yaml` (`sync` → `HEAD`, `full` → `GT_BASE_REF`/`HEAD~1`) и FAIL'ит рост ранга при том же `check`/`check_kind` и нетронутых target-файлах (`references/verifier.md` §14). Предел: косметическое касание теста, его ослабление, подмена `check` на столь же слабый или переименование `id` claim'а (новый `id` = новый claim, сравнивать не с чем) — по-прежнему дело adversarial verify + judge (I.3/I.3.5) и сэмплирования на `audit`. SC-b (authoring-time «check был красным хотя бы раз») остаётся обязательным.
- **Ровно 3 `kind` (D6): `status` | `live_state` | `out_of_repo`.** `attested` не вводится и не появится (A1, R1). `status` всегда доказан `check`, `live_state` — только `command`, никогда сохранённым значением (A3).
- **`debt` всегда с `ref`** на существующий claim; `debt.state: resolved` невалиден, пока `claims[ref].status` в `{stub, partial, absent}` — иначе долг становится второй правдой.
- **`edges:` (кросс-сервисные/кросс-репо) всегда с `probe:`.** Декларация ребра без пробы — unfalsifiable запись, запрещена (A1/R2).
- **Canary — атрибут реального claim'а (D20), не отдельная запись.** Минимум 1 на репо, `canary: true` + `mutation:{file,find,replace}`.
- **`path`/`check` никогда не резолвятся внутрь `docs/_human/**`.** Три независимых слоя защиты (правило в роутере, бриф-исключение, структурный гвард в `check_shape`) — см. `references/schema.md`.
- **Ноль identity-токенов в артефактах, идущих в репозиторий (D16).** Верификатор (`check_no_banned_words`, `--mode=full`) — обязательная проверка, не опция; тот же список слов — в каждом промпте суб-агента (`references/prompts.md`).
- **Никогда не коммитить/пушить без явного запроса пользователя** — существующая конвенция, скилл её не переопределяет.
- **Выделенный git worktree на прогон + `git worktree list` первым делом (D14.4).** Не работать в чужом занятом worktree.
- **Один `STATUS.yaml` на один git-репозиторий (`git rev-parse --show-toplevel`), никогда на умбреллу (§8.2 спеки).** Если запущено из зонтичной папки без своего `.git` — детект по отсутствию `.git`, меню найденных вложенных `.git`, не тихое создание файла не там.
- **Хуки — project-local, committed, ТОЛЬКО если `.claude/` уже трекается в репо (D25).** Проверка: `git ls-files .claude`. Трекается → committed хуки в `<repo>/.claude/hooks/` + запись в `<repo>/.claude/settings.json`. Не трекается (корп-репо без `.claude/`) → хуки в `tools/ground_truth/hooks/`, настройки в `.claude/settings.local.json` (untracked), CI — единственный committed backstop.

## 4. Режимы

### 4.1 `init` — репозиторий без контракта

Общий скелет: `pm` (research → reconcile → adversarial → judge → decider) + `doc-model` (research → модель → apply → verify → fleet-probe). Модели и промпт-скелеты — `references/prompts.md` (имя скелета указано в колонке «Промпт»).

| Фаза | Стадия | Что | Роль→модель | Выход | Verify | Промпт |
|---|---|---|---|---|---|---|
| I.1 | `pipeline/workflows/i1-research.js` | Research sweep, параллельно read-only | 4 finder'а (§2) | `research/{inventory,claims,codemap-*,debt-candidates}.json` | 4 отчёта на диске | finder-inventory/claims/codemap/debt |
| I.1 secret-правило | `i1-research.js` (debt finder) | key-looking литерал в tracked-коде (private key, token, password инлайном) → отдельная `debt`-запись | finder — debt-scanner | запись в `debt-candidates.json` с `path:line` | правило не зависит от редакции доков: секрет в трекаемом коде = долг, даже если дока о нём молчит и claim уже покрыт другой записью | finder-debt |
| I.2 | `i2_build_slices.py` → `i2_build_args.py` → `i2-i35.js` (фаза Reconcile) (лессон п.14: немаппированный бакет получает `component` = сам путь области, никогда предложение вроде «unmapped items under X») | Reconcile per component, параллельно | sonnet-таблица → opus (D26) | черновой claim + evidence обе стороны | evidence_for/against непустые | reconcile-per-component |
| I.3 | `i2-i35.js` (фаза Adversarial) | Adversarial verify, асимметрично | opus prosecutor+defender, high (D34) | verdict + findings | 3-vote только при ↑confidence/code-fix | adversarial-prosecutor/defender |
| I.3.5 | `i2-i35.js` (фаза Judge) → `i35_dump_judged.py` → `i35_assemble_status.py` | Judge/synthesis, батч 5–8 | opus, xhigh (D34) | final status/check + route | ровно 1 route на claim; обязательная проверка пар claim'ов с одинаковым `path` И одинаковым `check` при разных `status` — либо merge, либо перекрёстные `note` в обеих записях | judge-synthesis |
| I.2/I.3.5 note-гигиена | `i2-i35.js` / `i35_assemble_status.py` | `note` не цитирует `CLAUDE.md:N` — I.8 переписывает `CLAUDE.md`, ссылка протухает; цитировать код/доки с устойчивыми якорями | автор claim'а + judge | `note` без битых ссылок | plain-скаляр YAML в `note` не терпит «: » (верификатор даёт exit 3) — писать через «;»/«,» либо quoted-скаляр; `grep -n 'CLAUDE.md:' STATUS.yaml` пуст | reconcile-per-component, judge-synthesis |
| I.4 | `i4_classify_briefs.py` → `i4_build_args.py` → `i4.js` → `i4_apply.py` (`--write` пересобирает claims/debt репо-`STATUS.yaml` из `claims-judged.json` — ручные правки репо-копии после I.3.5 теряются, править judged; пробы/тесты/канарейки скоуплены по брифу, не сканируют дерево целиком — лессон п.13 про repo-wide сканеры относится к I.1-finder'ам, не сюда) | Code-fix pass (D12), только self-inconsistency | opus (effort medium), TDD red→green | diff + forbidden_action_taken=false | main лично читает каждый `git diff` | code-fix-dev |
| I.4 budget (D24) | `i4_build_args.py --canaries N` | ≤1 characterization-тест на implemented/partial claim, потолок ~25 тестов за прогон | — | тест либо wiring-only проба, либо `debt` «no guard» | main следит за потолком по ходу фазы | code-fix-dev |
| I.5 | `i5_build_briefs.py` (брифы конфликтов из `claims-judged.json`: `research/i5/{docs,code}/<slug>.json`) → `i5_build_args.py` → `i5.js` | Doc reconciliation + карантин | opus (effort medium), 1 документ/агент | changes + verdict keep/quarantine/delete | список удалений/карантина утверждён владельцем ДО действия | doc-fix-editor, quarantine-classifier |
| I.5 мувы | `i5.js` (doc-fix editor) + `i5_comment_guard.py` | перенос/удаление файла — только `git mv` / `git rm`, голый `mv`/`rm` запрещён | executor фазы | индекс = состояние рабочего дерева | `git status` без пар ` D`+`A`; после каждого `git mv`/`git rm` — `verify.py --mode=sync` и правка claim'ов, чьи `path` указывают на перенесённые файлы, ДО I.8/I.10 (иначе каскад FAIL через `wiring_paths_exist`) | doc-fix-editor |
| I.5 review-loop | `i5.js` (Docs pipeline) | reviewer-бриф и fixer-бриф несут один и тот же раздел scope дословно | reviewer opus + fixer opus (effort medium) | согласованный verdict | разошедшийся scope = раунды FAIL по одному пункту; 2 раунда FAIL подряд → выход из цикла decider-override'ом main, не третьим раундом | doc-fix-editor |
| I.5 объём (D39) | `i5.js` (второй Workflow-запуск) | >60 документов на фазе → review-фаза выносится в отдельный Workflow | main | два workflow вместо одного | очередь агентов в одном workflow — cap 8 | — |
| I.6 | `i6_build_args.py` → `i6.js` | Mapping-audit | opus, независимый | keep-inline/moved-to/link/drop+причина на 15–20 утверждений | default-FAIL при сомнении | mapping-audit-reviewer |
| I.7 | — (существующий скилл, вызов напрямую, не пайплайном) | doc-model (опционально, D2) | существующий скилл | человекочитаемый слой поверх решённого контракта | вызывать ТОЛЬКО после того, как контракт установил правду | — (см. `references/doc-model-bridge.md`) |
| I.8 | — (main зовёт opus-агента напрямую, без стадии/workflow) | Navigation layer build | opus, effort medium | `.claude/rules/ground-truth.md`, routing-таблица | cold-start bundle «после» посчитан: `CLAUDE.md` + `.claude/rules/ground-truth.md` (всё, что читается при старте); `STATUS.yaml` в бандл НЕ входит — доступ grep'ом | — |
| I.9 | `i9_install.py` (лессон п.12: verifier гоняет `.py`-пробы через `sys.executable`, не shebang; JS-тест без `test`/`it`/`describe(` резолвится fallback'ом на quoted-литерал имени; лессон п.15: `meta.banned_words.exclude{path,why}` — штатный ход, когда FAIL ловит настоящее слово-триггер на production-файле, не повод гасить проверку; лессон D61: job доказывается в CI-образе репо через docker ДО push, `needs: []`, наследует setup тестового job'а, `cache:`-path внутри checkout → `.gitignore` (факт `ci_cache_unignored`), пробы — `shutil.which`→77) | Verifier + hook + CI wiring | opus (effort medium), механика | `tools/ground_truth/*`, hooks, CI job | `verify.py --mode=full` зелёный (не `sync`: wiring ловится только полным прогоном); пробы, фиксирующие полный список CI-джобов, правятся в том же шаге, где добавлен job `verify-status-contract`; см. §5 установки | — |
| I.10 | `tools/ground_truth/gt_mutation_selfcheck.py --allow-dirty` (лессон п.19: флаг нужен, потому что selfcheck запускается до коммита в грязном дереве — I.4/I.5 трогают те же файлы, что и канарейки; без флага скрипт остаётся строгим для Stop-хука и CI) → `i10_build_args.py` → `i10-fleet.js` | Acceptance: mutation-selfcheck, затем fleet-probe | скрипт selfcheck → 5× opus navigator (effort medium) + opus grader (D34) | canary-flip red + pass/fail на 5 целей | SC-b, затем SC-d; selfcheck строго ДО навигаторов, не параллельно — мутации на секунды меняют файлы, которые навигаторы читают (ложный ответ вроде «DAYS_BEFORE_DUE = 30» у канарейки из примера схемы) | fleet-probe-navigator/grader |
| I.11 | — (main, `## Outcome`) | Reconcile (main) | main | `## Outcome` заполнен (§6) | slug зарегистрирован/закрыт в `_active.md` | — |

**Инварианты запуска фаз.**

- **Модель и effort судейской роли — явно в каждом `agent()`.** В workflow-скрипте прогона строка судейской роли выглядит как `agent(prompt, {model: 'opus', effort: 'xhigh'})`; пропущенный `model` = дефолт субагентов (`CLAUDE_CODE_SUBAGENT_MODEL`, с 2026-09-23 = opus) без явного effort, `'fable'` в Workflow не резолвится (D34, §2).
- **ETA перед каждой фазой (D38).** Main пишет владельцу одну строку «фаза X запущена, ETA ~N мин» до спавна агентов — иначе владелец ждёт вслепую. Базис оценки: прогон на `service-b` (`service-a`…`service-d` — обезличенные имена репозиториев, на которых шли прежние прогоны) — I.5 doc-reconcile 81 мин, I.10 acceptance 108 мин; fleet-probe отдельным Workflow ~5 мин на раунд (лессон п.20 — прежняя оценка ~25 мин мерила повторные раунды, не один прогон: на `service-a` один раунд из 5 навигаторов + opus/high грейдера занял 3 м 45 с). Полный разбор по фазам последнего прогона, включая пост-I.5 цепочку и raise-vote — `references/pipeline.md`.

### 4.2 `audit` — периодическая ре-валидация

Вход: существующий `STATUS.yaml`, дрейф от последнего коммита, тронувшего `STATUS.yaml`, полное сканирование coverage. Стадия `audit_scope` (`references/pipeline.md`, раздел `audit`) решает дёшево и детерминированно: `verify.py --mode=full` зелёный + под coverage roots нет ни изменённых, ни untracked файлов относительно этой базы + `last_audited` не старше `--max-age-days` (30) → **короткое замыкание: finder/reconcile/adversarial/judge не спавнятся**. Иначе — I.1–I.6 в scoped-режиме (finder смотрит только `drift_files`/`touched_claims` из `audit-scope.json`), но adversarial verify **дополнительно сэмплирует N случайных нетронутых claim'ов** (`sample_claims`, seed фиксирован), не только тронутые (иначе нетронутый claim гниёт незамеченным). В обоих ветках бегут всегда: canary-flip (`gt_mutation_selfcheck.py`, установленная копия), **fleet-probe (D23 — на каждом audit, не только на init)** и `audit_close`, который в конце ставит `meta.last_audited` = дата прогона (только при зелёном `verify`, только с `--write`). Переход `advisory → blocking` — только ручное решение main на audit, никогда автоматический триггер по времени/числу прогонов; смена поля — обычный коммит.

### 4.3 `sync` — лёгкий режим, триггер Stop hook

Вход: `git diff` текущей/последней сессии. Один opus-агент (effort medium, `sync-delta-agent`, `references/prompts.md`): предлагает минимальную дельту `STATUS.yaml`. Без adversarial/judge для обычного случая; неуверен → `ESCALATE:`, не угадывает. Main читает дельту (маленькая, дёшево), утверждает/правит. Verify: `verify.py --mode=sync` зелёный после применения дельты — это и есть то, что проверяет Stop hook (§5, `references/hooks.md`). Тот же `sync`-прогон в Stop-хуке включает status-raise guard против `HEAD`: поднять `status` в дельте, не тронув target `check`'а, — FAIL прямо в хуке, до конца хода. После верификатора тот же хук зовёт `gt_session_guard.py --mode hook`: он реально гоняет `check`'и тронутых claim'ов, требует `mutation` у новых/поднятых `implemented|partial` и ищет тест на новые определения в изменённых файлах (`references/hooks.md` §3.1).

Severity-политика (D70): в `--mode=sync` (этот Stop-хук) FAIL'ят только красный `check`, status-raise без правки `check`'а, `debt` → `resolved` без доказательства (правка `check`'а claim'а по `ref`) и shape-ошибки; протухший `path`, непокрытый файл (`coverage`), сдвиг строки `check`/remap и цитата (`cited-line`), не найденная на своей строке, печатаются `[WARN]` с готовой `fix:`-командой и Stop не блокируют. В `--mode=full` (CI) те же находки — `[FAIL]`: WARN в sync не повод откладывать, следующий CI-прогон его остановит. Механика: сдвиг строки/remap в sync печатает не `verify.py`, а сам Stop-хук (`ground-truth-sync-check.sh`) — `[WARN]` с готовой командой `--apply`; в CI то же самое дополнительно ловит отдельный шаг `remap_line_refs.py --check`.

## 5. Установка артефактов из `templates/`

Установка выполняется одной командой: `python3 <skill>/pipeline/gt.py i9_install --run run.json --write` (без `--write` — сухой прогон, только план; `--facts` печатает собранные факты и выходит без записи). Стадия сама собирает факты ниже и раскладывает `templates/` по таблице; ручная раскладка построчно по таблице остаётся резервом на случай, когда стадию нельзя запустить.

**Факты собрать ПЕРЕД установкой — командами, а не по памяти:** `git ls-files .claude` (трекается ли `.claude/` — определяет committed vs local хуки, D25); наличие `.github/workflows/*.yml` / `.gitlab-ci.yml` (job добавляется в существующий файл, не создаётся параллельный); наличие `.pre-commit-config.yaml` (запись добавляется, только если файл уже есть); какой test runner реально стоит — для Python не только `pytest.ini`, а любой из: `pytest.ini`, `pyproject.toml` с секцией `[tool.pytest.ini_options]`, `setup.cfg` с секцией `[tool:pytest]`, `tox.ini` с секцией `[pytest]`, иначе fallback — наличие каталога `tests/`; для Go/JS — `go.mod`/`package.json`+jest/vitest как и раньше; для Java — `pom.xml` + `src/test/java`. Определяет `check_kind`-дефолт и bridge-тест.

| Файл в `templates/` | Куда копируется | Переименование/права |
|---|---|---|
| `STATUS.yaml.template` | `<repo>/STATUS.yaml` | снять `.template`, заполнить claims по факту репо |
| `contract_lib.py` | `<repo>/tools/ground_truth/contract_lib.py` | как есть |
| `verify.py` | `<repo>/tools/ground_truth/verify.py` | как есть |
| `blast_radius.py` | `<repo>/tools/ground_truth/blast_radius.py` | как есть |
| `gt_mutation_selfcheck.py` | `<repo>/tools/ground_truth/gt_mutation_selfcheck.py` | как есть |
| `gt_session_guard.py` | `<repo>/tools/ground_truth/gt_session_guard.py` | как есть; зовётся Stop-хуком и CI (`--mode ci`) |
| `gt_edit_guard.py` | `<repo>/tools/ground_truth/gt_edit_guard.py` | как есть; зовётся PreToolUse-хуком |
| `gt_hook_state.py` | `<repo>/tools/ground_truth/gt_hook_state.py` | как есть; общий стейт-файл хуков (blast-ack, счётчики), зовётся из gt_edit_guard.py, blast_radius.py и хуков-шеллов |
| `coldstart_budget.py` | `<repo>/tools/ground_truth/coldstart_budget.py` | как есть |
| `probes/example_probe.py` | `<repo>/tools/ground_truth/probes/` | `chmod +x`, использовать как образец, не копировать буквально |
| `status_contract_test.py.template` | `<repo>/tests/test_status_contract.py` | снять `.template` |
| `status_contract_test.go.template` | `<repo>/<pkg>/status_contract_test.go` | снять `.template`, только для Go-репо/пакетов |
| `status_contract.test.ts.template` | `<repo>/<path>/status_contract.test.ts` | снять `.template`, только если в репо уже стоит jest/vitest |
| `status_contract_test.java.template` | `<repo>/src/test/java/<pkg>/StatusContractTest.java` | снять `.template`, только для Java/Maven-репо, `PACKAGE_NAME` подставляется i9 |
| `ground-truth-sync-check.sh` | `<repo>/.claude/hooks/` (`.claude/` трекается, D25) или `<repo>/tools/ground_truth/hooks/` (не трекается) | `chmod +x` |
| `ground-truth-session-start.sh` | туда же, где sync-check | `chmod +x` |
| `ground-truth-edit-guard.sh` | туда же, где sync-check | `chmod +x` |
| `ground-truth.rule.md` | `<repo>/.claude/rules/ground-truth.md` | без `paths:`-frontmatter, always-on (D35); плейсхолдер `{{PY}}` → `.venv/bin/python`, если файл есть, иначе `python3` |
| `settings-hooks.json` | записи `Stop`/`SessionStart`/`PreToolUse` мёржатся в `<repo>/.claude/settings.json` (`.claude/` трекается) или `.claude/settings.local.json` (не трекается) | не копировать файлом целиком — мёржить массивы, см. ниже |
| `ci-github.yml` | новый `.github/workflows/ground-truth.yml`, если голого CI ещё нет | — |
| `ci-github-job-addition.yml` | job `verify-status-contract` вставляется в `jobs:` существующего `.github/workflows/*.yml`, если такой уже есть | не создавать параллельный workflow-файл |
| `ci-gitlab-addition.yml` | секция внутрь существующего `.gitlab-ci.yml` | не переписывать файл целиком; `needs: []`; `before_script` — как у тестового job'а репо (anchor, placeholder-файлы) |
| `pre-commit-addition.yaml` | запись `ground-truth-verify` в `repos:` существующего `.pre-commit-config.yaml` | только если файл уже есть в репо (см. факты выше) |

**settings.json merge:** если `.claude/` трекается (хуки committed, D25), добавить записи в `Stop`, `SessionStart` и `PreToolUse` массивы `<repo>/.claude/settings.json` рядом с уже существующими (не заменять массив); если не трекается, те же записи в `.claude/settings.local.json`. **pre-commit:** запись добавляется только если `.pre-commit-config.yaml` найден фактом выше — не создаётся с нуля ради этого скилла. **CI:** если `.github/workflows/*.yml` есть — job внутрь него по конвенции репо; если голый CI отсутствует — создаётся `ci-github.yml` как отдельный файл (Phase I.9 не молча принимает отсутствие CI).

## 6. Приёмка SC-a…SC-j и Outcome

- **SC-a** `STATUS.yaml`+верификатор существуют, зелёные в нативном раннере И standalone (`--mode=full`).
- **SC-b** Mutation-sensitivity: canary-flip надёжно валит верификатор (фактически прогнано) + каждый новый/изменённый claim несёт authoring-time доказательство «check был красным хотя бы раз», отревьюенное adversarial-проходом.
- **SC-c** Coverage two-way: ноль orphan-кода в scope, ноль фантомных `check`.
- **SC-d** Fleet-probe 5/5: свежие навигаторы отвечают на «поменять X» ссылкой на верный id за ≤3 чтения; независимый senior grader подтверждает по `blast_radius.py`+`STATUS.yaml`, не по самоотчёту.
- **SC-e** Cold-start bundle «после» ≤ «до» в байтах. Бандл = `CLAUDE.md` + `.claude/rules/ground-truth.md`, то есть всё, что агент читает при старте сессии; `STATUS.yaml` в бандл НЕ входит — он читается точечно grep'ом, и в замер `coldstart_budget.py FILE...` подаются только файлы бандла. mapping-audit — ноль немаппированных потерь.
- **SC-f** CI job подключён и зелёный на реальной платформе (не только локально).
- **SC-g** Скан на identity-слова (`--mode=full`) чист на tracked-файлах пушенного репозитория.
- **SC-h** (антигейминг) Отчёт «долг = 0» на репозитории с известными заглушками — сам по себе красный флаг, требует обоснования.
- **SC-i** Каждая `debt`-запись несёт `ref` на существующий claim; ни одна не `resolved`, пока claim в `{stub,partial,absent}`.
- **SC-j** Каждый `edges:` несёт рабочий `probe:`.

```markdown
## Outcome (ground-truth run: <mode> · <date>)

- claims: N всего (implemented: a, partial: b, stub: c, absent: d, design-only: e, live_state: f, out_of_repo: g)
- debt: N записей (severity breakdown), закрыто при этом прогоне: N
- coverage: two-way чисто: да/нет; orphan-код: [...]
- verifier: green (нативный раннер) / green (--mode=full); mutation-selfcheck: red-on-flip подтверждён
- fleet-probe: N/5 pass (грейдер: <модель>), провалы: [...]
- cold-start bundle: до X байт -> после Y байт
- mapping-audit: N утверждений замаплено, 0 потеряно без причины (либо: потеряно [...])
- doc-model вызывался: да/нет, почему
- дубли удалены: [список], карантин docs/_human/ пополнен: [список] -- оба утверждены владельцем ДО переноса/удаления
- code-fix кандидаты, отклонённые от auto-fix (решение владельца по D12): [...]
- время по фазам: I.1 ... I.11 (мин), всего N мин
- уроки для skill v(n+1): [...]
```

## 7. Открытые решения: скилл сам vs спрашивает владельца

| Решение | Кто решает | Почему |
|---|---|---|
| Список точных дублей на физическое удаление | **владелец** | D13/D22: скилл готовит список с однострочной причиной на файл, `git rm` только после утверждения |
| Состав `docs/_human/` (что туда переносится) | **владелец** | D13 буквально: «решение по составу — моё» |
| Имплементация найденной заглушки | **владелец, всегда откладывается** | D12: решение о фиче, не факт о коде; скилл никогда не делает это сам даже без явного вопроса — фиксирует кандидата в Outcome |
| Переход `advisory → blocking` | **main (decider) на audit** | D21/§3.2 спеки: ручное решение по факту чистых прогонов, не автоматический триггер по времени; порог — открытый вопрос владельцу на будущее (не блокирует текущий прогон) |

### Изменения после прогона на `service-b`

- **D33** (урок 2) — WARN «debt is open but claim already implemented» остаётся только для записей с `severity: blocking`; normal/cosmetic долг на implemented claim — норма, без WARN.
- **D34** (урок 1) — adversarial prosecutor/defender, judge/synthesis и fleet-probe grader идут на opus (judge/synthesis effort xhigh, остальные high), модель и effort передаются явно в каждом `agent()`; fable в этих ролях — только прямым вызовом Agent tool.
- **D35** (уроки 9, 19) — rule-файл без frontmatter `paths:` (always-on); в шаблоне плейсхолдер `{{PY}}`, I.9 подставляет `.venv/bin/python` при наличии файла, иначе `python3`; hook-скрипты берут `GT_PYTHON`, иначе `$root/.venv/bin/python`, иначе `python3`; в CI интерпретатор и зависимости — те же, что у job тестов проекта.
- **D36** (урок 11) — `_git_ls_files` получает `include_untracked` (по умолчанию False); banned-word scan в full mode зовёт его с `include_untracked=True` (`git ls-files --cached --others --exclude-standard`), прочие вызовы не меняются.
- **D37** (урок 10) — дочерние процессы `gt_mutation_selfcheck.py` и probe-шаблон с pytest стартуют с `PYTHONDONTWRITEBYTECODE=1` и `pytest -p no:cacheprovider`; после restore canary selfcheck удаляет `__pycache__` рядом с восстановленным файлом.
- **D38** (урок 23) — Outcome несёт строку «время по фазам», main перед каждой фазой пишет владельцу ETA с базисом `service-b` (§4.1).
- **D39** (урок 5) — при >60 документов на I.5 review-фаза выносится в отдельный Workflow (очередь агентов в одном workflow — cap 8).

## 8. Указатели на `references/`

- `references/schema.md` — полная схема `STATUS.yaml`: kind/поля, check-синтаксис по языкам, coverage-правило, debt/edges/canary в деталях. Читать перед написанием/правкой claims.
- `references/verifier.md` — дизайн верификатора: что проверяет каждая функция, sync/full, allowlist no-identity-words, git-blame-staleness. Читать перед Phase I.9/при отладке `verify.py`.
- `references/hooks.md` — enforcement-цепочка: Stop/SessionStart/CI/pre-commit, семантика exit-кодов, урок decider-gate. Читать перед установкой хуков (§5) и перед переключением `advisory → blocking`.
- `references/prompts.md` — промпт-скелеты всех ролей init/audit/sync. Читать перед спавном любого суб-агента фазы.
- `references/doc-model-bridge.md` — когда/как звать `doc-model` (Phase I.7), пойнтер на PII-правила. Читать перед решением звать doc-model или нет.
