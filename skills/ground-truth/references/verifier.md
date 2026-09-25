# verifier.md — что проверяет verify.py/contract_lib.py, как читать FAIL/WARN

Отвечает на вопрос: «что именно делает каждая проверка, почему `sync` и `full` — разные наборы, что значит exit-код, почему банится это слово / считается протухшим этот `live_state` / молчит эта проба». Читать при разборе вывода `verify.py`, при написании нового claim'а (какая проверка его затронет), при настройке Stop hook/CI (какой режим где вызывать), при работе с `gt_mutation_selfcheck.py`.

Публичный интерфейс (не менять сигнатуры): `verify.py --mode=sync|full [--root DIR]`; `contract_lib.py`: `load_contract(root)`, `check_shape(root, data)`, `check_coverage(root, data)`, `check_collectible(root, data, mode)`, `check_links(root, data, mode)`, `check_assertion_density(root, data, mode)`, `check_debt_coverage(root, data)`, `check_canary_roots(root, data, mode)`, `check_audit_due(root, data, mode)`, `check_no_banned_words(root, data)`, `check_stale_live_state(root, data)`, `check_status_raise(root, data, mode)`, `run_claim_check(root, claim, timeout=180) -> (ok, message, skipped)`, `run(root, mode) -> list[Issue]`, `Issue(severity, claim_id, message)`.

---

> Текущая политика обязательности — SKILL.md/process.md. Полный green означает только выполненные checks; предупреждения и границы читать отдельно. Дорогой полный конвейер не обязателен для обычной работы.

## 1. Два режима — что входит в каждый

`run(root, mode)` — единственная точка входа, которую вызывает `verify.py`. Хук/CI никогда не дёргают отдельные `check_*` напрямую — только через `run()`, чтобы набор проверок не мог случайно разъехаться между вызывающими местами.

| Режим | Набор проверок | Когда вызывается | Почему такой набор |
|---|---|---|---|
| `sync` | `check_shape` + `check_coverage` + `check_collectible` + `check_links` + `check_assertion_density` + `check_debt_coverage` + `check_canary_roots` + `check_audit_due` + `check_status_raise` | Stop hook, на каждом ходе сессии | всё дёшево и не требует сети/git-blame/pytest-раннера — задержка должна быть субсекундной (spec §2.6) |
| `full` | sync + banned words/live state/citations и реальное исполнение всех объявленных pytest/Go/JS/JUnit/probe checks | audit, CI; SessionStart использует sync | Дороже: реальные проверки в окружении проекта |

**Инвариант:** check_collectible вызывается в sync и full. В sync — статическая резолюция, в full — резолюция и реальное исполнение; существование теста не означает, что он прошёл.

**Severity:** check_canary_roots — WARN sync / FAIL full. check_audit_due — WARN в обоих режимах: календарь не доказывает дефект. Остальные правила имеют собственную классификацию.

## 2. Что проверяет каждая функция

| Функция | Что делает | `sync` | `full` |
|---|---|---|---|
| `check_shape(root, data)` | форма claim'а по `kind` (обязательные/запрещённые поля), закрытый словарь `status`, уникальность `id`, гвард `docs/_human/**` на `path`/`check` (`references/schema.md` §12) | да | да |
| `check_coverage(root, data)` | two-way: orphan-код (нет claim'а) + протухший claim (нет файла) — `references/schema.md` §5 | да | да |
| `check_collectible(root, data, mode)` | Статическая резолюция и, в full, реальное исполнение checks; required skip — FAIL неполной проверки | да | да, сильнее |
| `check_links(root, data, mode)` | `depends_on`/`see_also` резолвятся в существующие id; `edges[].from` резолвится, `edges[].probe` существует+исполняем; `debt.ref` резолвится и подчиняется правилу «resolved невалиден при stub/partial/absent» (`references/schema.md` §10) и зеркальному «долг открыт при `implemented`-claim'е → WARN только при `severity: blocking`» (D33); уникальность `debt.id`. В `full` — дополнительно сканирует tracked `*.md` на `STATUS.yaml#<id>`-ссылки и проверяет их резолвимость (D27, `references/schema.md` §13) | да (без md-скана) | да (+ md-скан) |
| `check_assertion_density(root, data, mode)` | AST-обход каждого уникального файла из `check_kind: pytest`-claim'ов: тестовая функция с нулём `assert`/`self.assert*` → WARN «check существует и коллектируется, но ничего не может провалить»; если на этот тест ссылается claim, которого нет в базовой версии `STATUS.yaml`, — FAIL (§6) | да | да |
| `check_debt_coverage(root, data)` | claim в `stub`/`partial`/`absent` без записи долга `ref: <id>` в состоянии `open`/`accepted` → FAIL; `design-only` освобождён — §18 | да | да |
| `check_canary_roots(root, data, mode)` | coverage-root, где есть `implemented`/`partial`-claim'ы, но ни на одном нет `canary: true` — WARN в `sync`, FAIL в `full` — §19 | да (WARN) | да (FAIL) |
| `check_audit_due(root, data, mode)` | Возраст аудита / объём изменений — повод для ревью, не доказательство дефекта | WARN | WARN |
| `check_status_raise(root, data, mode)` | сравнивает статусы claim'ов с базовой версией `STATUS.yaml` (`sync` → `HEAD`, `full` → `GT_BASE_REF`/`HEAD~1`): повышение ранга при неизменённом `check` и нетронутых файлах check'а → FAIL — §14 ниже | да | да |
| `check_no_banned_words(root, data)` | сканирует `git ls-files --cached --others --exclude-standard` (tracked + untracked, не игнорируемые; только этот git-репозиторий) на идентити-токены и бузворды — §3 ниже (D36) | нет | да |
| `check_stale_live_state(root, data)` | `git log -1 -L <start>,<end>:STATUS.yaml` по каждому `live_state`-claim'у — §4 ниже | нет | да |

`check_assertion_density` — в `sync` тоже (дешёвая, чисто AST, без субпроцессов) — постоянно включённая структурная линза на класс «check без зубов», не только на разовом мутационном упражнении (spec §2.6).

## 3. Банword-политика (D16)

Сканирует `git ls-files --cached --others --exclude-standard` — tracked-файлы **плюс** untracked, не подпадающие под `.gitignore` (D36), только текущий git-репозиторий, исключая `docs/_human/**` (карантин не подчиняется этому правилу — человеческая проза изнутри карантина не должна ломать гейт). Пропускает бинарные файлы (null-byte в первых 8КБ) и файлы больше 2 МБ.

**Почему untracked тоже (D36).** `_git_ls_files(root, include_untracked=False)` по умолчанию отдаёт только трекаемый набор; `include_untracked=True` передаёт единственный вызывающий — banned-word scan в `full`. Без этого файл, ещё не прошедший `git add`, не попадал в скан вообще: гейт зелёный, слово уезжает в первый же коммит — то есть проверка молчит ровно в момент, когда её ещё дёшево починить. Остальные множества файлов не меняются: md-скан ссылок `STATUS.yaml#<id>` в `check_links` идёт по трекаемому набору намеренно (ссылка из неотслеживаемого черновика ничего не обещает), а coverage и status-raise guard строят свои списки сами (обход `coverage.roots` по диску, `git status --porcelain`) и untracked-файлы и так видят.

**Следствие для CI (D61).** Кэш, который раннер восстанавливает внутрь checkout (`$CI_PROJECT_DIR/.pip-cache`, `node_modules` под `actions/cache`), — untracked-каталог, и метаданные сторонних пакетов в нём полны стоп-слов (`service-c`: 40 FAIL на первом реальном прогоне). Лечится не исключением в сканере, а `.gitignore`: `i9_install` собирает такие пути фактом `ci_cache_paths` / `ci_cache_unignored` и дописывает их на `--write`.

Два непересекающихся набора:

| Набор | Токены | Severity | Регистр |
|---|---|---|---|
| **FAIL — идентичность** | `\bClaude\b`, `\bAnthropic\b`, `\bChatGPT\b`, `\bGPT-?\d`, `\bCopilot\b`, `\bLLM\b`, `Co-Authored-By`, `\bAI\b`, `"AI-assisted"`, `"AI-generated"`, `"coding agent"`, `"language model"` | fail | **регистрозависимо** (`\bAI\b` не матчит «Ai» или «ai» внутри обычного слова) |
| **WARN — бузворды** | delve, leverage, comprehensive, robust, seamless, streamline, consolidate, modernize, enhanced, utilize, facilitate | warn | регистронезависимо |

**Почему по намерению, а не по голому слову (D16).** Плоский `\b(agent|model)\b`-класс regex валит собственный легитимный технический контент — claim `node_agent_health_prober_stub`, `component: "node-agent-health Prober"` из прототипного репозитория содержат банящуюся подстроку, будучи нормальными техническими именами. Гейт, который красит уже закоммиченный легитимный код, учит следующие сессии его обходить или отключать — тот же класс поражения, что decider-gate, только с другой стороны (ложное срабатывание вместо пропуска, spec §2.6). Поэтому:

- голые `\bagent\b`/`\bmodel\b` **не сканируются вообще** — не входят ни в FAIL, ни в WARN набор (User-Agent, ssh-agent, `models.py` — легитимные, частые);
- совпадение, целиком укладывающееся в уже объявленное в этом же `STATUS.yaml` значение `id`/`component`/`meta.repo`, пропускается автоматически;
- построчный эскейп `# gt-allow: <причина>` в комментарии той же строки — гасит совпадение на этой строке независимо от набора. Работает в любом языке комментариев, где после `gt-allow:` не требуется закрывающего синтаксиса — сканер ищет саму подстроку `gt-allow:` в строке, не парсит комментарий.

**Если `git ls-files` не смог отработать** (нет `git` в `PATH`, не git-репозиторий, ненулевой exit) — это **FAIL** `could not enumerate tracked files, banned-word scan did not run: <причина>`, не тихий пропуск проверки с пустым списком файлов. Молчаливое «нет файлов — нечего сканировать» превращало бы сломанное окружение в зелёный прогон ровно там, где гейт больше всего нужен (SC-g на пушенном репозитории). Тот же принцип и то же исключение (`GitUnavailable`) применяется в `check_links` для `full`-режимного скана `STATUS.yaml#<id>`-ссылок по `*.md` (§2 таблица выше) — обе точки сканирования `git ls-files` обязаны падать явным FAIL, а не возвращать `[]`.

## 4. Stale `live_state` через `git log -L` (D27)

Не через ручную дату в тексте (регрессия одного из черновиков — сессия правит прозу заметки, дата рядом не трогается, гниёт незамеченным). Алгоритм `check_stale_live_state`:

1. Найти в тексте `STATUS.yaml` строку, содержащую `- id: <claim_id>`, для каждого `live_state`-claim'а.
2. Блок claim'а = от этой строки до строки перед следующей `- id:` или следующим top-level ключом файла (`edges:`, `debt:`), если claim — последний в `claims:`.
3. `git log -1 --format=%ad --date=short -L <start>,<end>:STATUS.yaml` — дата последнего РЕАЛЬНОГО изменения именно этого диапазона строк.
4. Нет вывода (файл не закоммичен / блок только что появился в рабочем дереве, не в истории) → **skip**, не WARN, не FAIL.
5. Дата старше `meta.stale_after_days` (по умолчанию 30) → **WARN** `live_state note last touched <date>, recheck it`. Никогда FAIL — нудж перепроверить командой из `command`, не поломанный контракт.

Только `full` (git log -L — тяжёлая операция, не для каждого Stop).

## 5. Пробы (`check_kind: probe`) и exit 77 (D18)

Проба — любой скрипт под `tools/ground_truth/probes/`, с контрактом:

| Exit code | Значение | Severity в отчёте |
|---|---|---|
| `0` | claim держится прямо сейчас | — (проверка пройдена) |
| `1` | claim сломан | **FAIL** |
| `77` | SKIP — необходимое окружение недоступно | full FAIL: не проверено, не обязательно сломано |

`sync`: проверяется только существование файла + (исполняемый бит **или** валидный shebang в первой строке) — проба не запускается. `full`: проба реально исполняется субпроцессом с рабочей директорией = корень репозитория.

**Почему full отказывает при 77.** Недоступный check не доказывает неисправность продукта, но не может дать полный зелёный gate. Нужен пригодный runner или отдельный отчёт об ограниченной проверке. Не подменять реальные production-проверки фиктивным успехом.

**Окружение ≠ claim.** Проба сообщает exit 77 при недоступной предпосылке. Full/CI откажут как неполная проверка; локальный sync остаётся доступен. Зависимости и ambient environment проверять в CI-образе; stub допустим только для явно моделируемой ветви, не как доказательство настоящего внешнего сервиса.

## 6. Assertion-density lint

Дешёвая, всегда включённая (`sync` + `full`) структурная проверка class'а «check без зубов»: AST-обход каждого уникального файла, на который указывает хотя бы один `check_kind: pytest`-claim, поиск `FunctionDef`/`AsyncFunctionDef` с именем `test_*`, подсчёт узлов `ast.Assert`, вызовов вида `self.assert*(...)` и, отдельно, `with pytest.raises(...)`/`with pytest.warns(...)`/`self.assertRaises(...)`/`self.assertWarns(...)` внутри тела функции — тест, характеризующий `stub`-claim через ожидаемое исключение (см. `beta_worker_stub` в `selftest/run.py`), реально доказывает поведение и не должен считаться «без зубов» только потому что в нём нет буквального `assert`. Ноль — **WARN** `<path>:<lineno>: <test_name> has zero assertions`.

**Новый claim — FAIL, а не WARN.** Severity зависит от того, был ли claim в базовой версии `STATUS.yaml` (та же база, что у status-raise guard'а, §14 — обе проверки берут её через один общий помощник, чтобы не разойтись в том, что значит «до этого изменения»). Claim, появившийся в этом изменении и сразу указывающий на тест с нулём ассертов, — FAIL `<check>: test has no assertions -- a new claim must ship a check that can fail`: контракт пишется здесь и сейчас, и требовать от него зубов дешевле всего именно сейчас. Тот же тест под claim'ом, который уже лежит в базе, остаётся WARN — иначе включение проверки покрасило бы весь накопленный долг разом. База не читается (нет `GT_BASE_REF`, первый коммит, git недоступен) → WARN для всех, не FAIL: неизвестно, какие claim'ы новые.

Это структурная линза, не замена мутационному самопроверу (§7): assertion-density лечит «теста нет вообще», mutation selfcheck — «assert есть, но проверяет не тот инвариант».

## 7. Mutation selfcheck — протокол `gt_mutation_selfcheck.py` (D20)

CLI: `python3 tools/ground_truth/gt_mutation_selfcheck.py [--root DIR] [--id CLAIM_ID] [--allow-dirty] [--strict]`.

Для каждого claim'а с `canary: true` (или только указанного `--id`, если задан):

1. **Отказ при dirty git.** Перед началом — `git status --porcelain -- <mutation.file>`; если файл уже имеет незакоммиченные изменения, скрипт отказывается стартовать (нельзя гарантировать чистый откат поверх чужих правок) — печатает причину, exit 1.
2. **Зелёная база, затем мутация.** Сначала выполнить исходный check; красная/пропущенная база не доказательство. Затем найти mutation.find и заменить первое вхождение. Автор выбирает однозначную достаточно длинную подстроку; скрипт не доказывает семантическую уникальность.
3. **Реальный прогон check'а.** Не резолюция, а фактическое исполнение — через тот же `run_claim_check` (§20), которым пользуются `check_collectible` и status-raise guard: `pytest` → `pytest <nodeid> -q -p no:cacheprovider`, `go_test` → `go test -count=1 -run '^<Name>$' ./<pkg>`, `js_test` → раннер из `package.json`, `junit` → `mvn -q -B -DskipITs=true -Dsurefire.failIfNoSpecifiedTests=true -Dtest=<SimpleClass>#<method> test` плюс разбор surefire-отчёта, `probe` → запуск скрипта. Дочерний процесс стартует с `PYTHONDONTWRITEBYTECODE=1` в env (D37). Canary можно вешать на claim любого из этих пяти видов, не только на pytest.
4. **Ожидание: FAILURE.** Мутация обязана провалить check. Если check остался зелёным — canary «без зубов»: печатается как непройденный canary, накапливается в итоговый вердикт.
5. **Откат и повторная проверка.** finally/обработчики сигналов восстанавливают исходный файл, затем check должен вновь стать зелёным. SIGKILL/сбой машины обходят обработчики — использовать изолированное дерево без параллельных читателей.

**Нет тулчейна — SKIP, не провал.** `run_claim_check` сообщает отдельным флагом, что check не удалось запустить (нет `go` в `PATH`, нет `node`/`npx`, в `package.json` не настроен ни один js-раннер). Такой canary печатается как `[SKIP] <id> : <причина>` и по умолчанию не роняет прогон: на машине без Go-тулчейна честный ответ — «не проверено», а не «canary без зубов». Флаг `--strict` превращает каждый SKIP в провал — им пользуется CI, где тулчейн обязан быть, и его отсутствие само по себе поломка окружения.

**Итог.** PASS требует зелёную базу → красную мутацию → восстановленный зелёный check. Без --strict exit 0 может включать честный SKIP; это ограничение, не доказательство. Для обязательной приёмки --strict.

Canary нужен в каждом coverage root с implemented/partial (§19). У каждого нового/повышенного implemented/partial также нужна mutation; canary — выбранное подмножество для периодической приёмки.

**Протухший байткод — источник ложного FAIL (D37).** pytest переписывает тестовые модули под assert-rewrite и кэширует результат в `__pycache__`, а валидность кэша определяет по mtime исходника — с точностью до секунды. Мутация, прогон и откат укладываются в ту же секунду, поэтому следующий запуск подхватывает `.pyc`, собранный по уже не существующему на диске тексту, и печатает провал теста, которого нет: на прогоне так «упал» `test_dead_code_removal`, зелёный при ручном запуске. Отсюда три меры разом — `PYTHONDONTWRITEBYTECODE=1` в env каждого дочернего процесса, `-p no:cacheprovider` у pytest, удаление `__pycache__` после отката. То же правило действует для пробы, которая внутри себя гоняет pytest (`templates/probes/example_probe.py`): проба-обёртка обязана ставить те же env и флаг, иначе унаследует ту же ложную красноту.

**Порядок в I.10: selfcheck ДО fleet-probe, не параллельно (урок прогона).** Selfcheck на секунды меняет содержимое исходников — ровно тех, которые в это же время читают навигаторы fleet-probe. Запущенные параллельно, навигаторы видят мутированное значение (на прогоне — `REWARD_DAYS = 7` у цели 1) и отвечают неверно: провал приёмки, вызванный измерительным инструментом, а не репозиторием. Фазы выстраиваются последовательно — mutation selfcheck отработал, `git status --porcelain` пуст, только после этого стартуют навигаторы.

## 8. Как pytest/Go/TS/Java-мосты вызывают верификатор

Сам верификатор не знает о языке репозитория за пределами `check_kind` — мосты вызывают его снаружи одинаково:

- **pytest-репозиторий**: bridge использует статические checks/sync; не вызывает full, если сам попадает в выбранные pytest checks (рекурсия).
- **Go-репозиторий**: `status_contract_test.go` шеллит `tools/ground_truth/verify.py --mode=sync` субпроцессом через `os/exec` (интерпретатор — по правилу ниже), `t.Fatalf` на ненулевой exit code — второй CI-шаг не нужен, `go test ./...` уже проверяет контракт вместе с остальным.
- **TS/JS-репозиторий**: тот же паттерн — bridge-тест шеллит `verify.py`, штатный test-раннер репозитория (jest/vitest, какой уже стоит) фейлится вместе с контрактом.
- **Java/Maven-репозиторий**: `src/test/java/<pkg>/StatusContractTest.java` (JUnit 5) шеллит `tools/ground_truth/verify.py --mode=sync` через `ProcessBuilder` из корня репозитория (корень ищется вверх по `STATUS.yaml`), `assertEquals(0, exitValue)` — `mvn test` фейлится вместе с контрактом, отдельный CI-шаг не нужен.
- **Голый CI, любой язык**: один универсальный job — install-шаг зависимостей тот же, что у job'а тестов репозитория, плюс `verify.py --mode=full` под python из `setup-python` (`references/hooks.md` §5). `pip install pyyaml` хватает только репозиторию, чей код не импортируется ни одной пробой и ни одним pytest-claim'ом.

Мосты никогда не переписывают логику проверок — только листингуют вызов `verify.py`/`contract_lib.run()` на языке своего раннера.

**Интерпретатор:** GT_PYTHON или .venv/bin/python проекта, иначе python3. Full исполняет tests/probes и импортирует код; системный Python без зависимостей не подходит. Настроить SessionStart/Stop/CI согласованно.

| Место | Как выбирается интерпретатор |
|---|---|
| rules-файл (`.claude/rules/ground-truth.md`) | плейсхолдер `{{PY}}` в командах; Phase I.9 подставляет `.venv/bin/python`, если файл существует, иначе `python3` |
| хук-скрипты (Stop, SessionStart) | `GT_PYTHON` из окружения → `$root/.venv/bin/python`, если исполняемый → `python3` (`references/hooks.md` §3) |
| CI-job | python из `setup-python`; зависимости — тем же install-шагом, что у job'а тестов (`references/hooks.md` §5) |

## 9. Как читать вывод

Формат строки: `[SEVERITY] <claim_id|-> : <message>` — `claim_id` = `"-"`, если проблема не привязана к одному claim'у (например orphan-код или банворд на произвольной строке файла).

| Exit code | Значит |
|---|---|
| `0` | ни одного FAIL (WARN допустимы) |
| `1` | хотя бы один FAIL |
| `3` | `STATUS.yaml` отсутствует или не парсится (`yaml.safe_load` упал / нет файла) — отличается от `1`, потому что это поломка самого контракта, не расхождение с кодом |

**Порядок разбора FAIL** (практический чек-лист): 1) прочитать `claim_id` — если `"-"`, искать причину в сообщении (путь файла, банворд); 2) свериться с `references/schema.md` по типу сообщения (`missing X for kind=Y` → §3.1, `orphan code` → §5, `check not collectible` → §4.2, `debt marked resolved but...` → §10); 3) починить на стороне, которая реально разошлась — если разошёлся код, чинить код (D12, только когда сломано относительно самого себя); если разошёлся контракт — чинить `STATUS.yaml`; 4) перезапустить `--mode=sync` локально перед коммитом, `--mode=full` — по расписанию `audit`/CI.

## 10. `Issue` — форма одной находки

```python
@dataclass
class Issue:
    severity: str   # "fail" | "warn" -- ничего третьего
    claim_id: str   # существующий id, либо "-" если находка не привязана к одному claim'у
    message: str    # человекочитаемая причина, конкретная (имя поля/путь/строка), не общая
```

Каждая `check_*`-функция возвращает `list[Issue]`; `run()` конкатенирует списки всех включённых в режим проверок в одном месте — ни одна функция не печатает и не решает severity сама, печать — забота `verify.py`.

## 11. `load_contract` и exit-код 3 — поломка контракта, не расхождение

`load_contract(root)` читает `<root>/STATUS.yaml` и `yaml.safe_load`'ит его. Два случая, которые НЕ доходят до `check_*`-функций вообще:

- файла `STATUS.yaml` нет по вычисленному `root`;
- `yaml.safe_load` бросает исключение (битый YAML — незакрытая кавычка, неверный отступ).

`verify.py` в этих случаях печатает причину и завершается **exit 3**, не 1 — это отличие принципиально: `1` значит «контракт синтаксически валиден, но не сходится с кодом» (обычная работа верификатора), `3` значит «сам контракт сломан как файл» — сигнал для человека читать `git diff STATUS.yaml`, а не для агента чинить код.

## 12. `--root` — как вычисляется по умолчанию

`verify.py` без `--root` вычисляет корень, поднимаясь от расположения самого скрипта: `tools/ground_truth/verify.py` → `parents[2]` → директория, где лежит `STATUS.yaml`. Явный `--root DIR` переопределяет это — нужен, когда верификатор запускается не из своего штатного места (например, тестовый прогон против fixture-репозитория в другом каталоге, `selftest/`).

Интерпретатор — независимая ось, скрипт его не выбирает: `verify.py` работает тем python'ом, которым его запустили, поэтому интерпретатор задаёт вызывающая сторона (`{{PY}}` в rules-файле, `GT_PYTHON`/`.venv` в хуках, `setup-python` в CI — D35, §8). С `--root` это не связано: прогон против fixture-репозитория идёт тем же интерпретатором проекта, что и обычный.

## 13. Что верификатор ловит и что остаётся за ним (обновлено D32)

`verify.py` доказывает форму и резолюцию, не семантику. Исторический разрыв: агент меняет `status: stub` → `status: implemented`, оставляя тот же `check` — тест существует, коллектируется, ассерты в нём есть, но ничего в нём не доказывает, что реализация реальна. `check_shape`/`check_coverage`/`check_collectible`/`check_assertion_density` в этом случае молчат.

Что изменилось (D32): **детерминированный `check_status_raise` (§14) теперь ловит повышение статуса без изменения check'а в том же изменении** — в Stop-хуке (`sync`, база = `HEAD`) и в CI (`full`, база = `GT_BASE_REF`). То есть ленивая или случайная ложь («поднял статус, тест не трогал») больше не проходит молча.

Расширение правила: повышение засчитывается и тогда, когда в этом же изменении тронут код, который claim покрывает (`path`), а не только файл его check'а — типичная честная последовательность «дописал реализацию под уже существующий тест» перестала быть ложным FAIL'ом. Плата за послабление — в `full` такой claim обязан предъявить зелёный check прямо здесь: `run_claim_check` (§20) запускает его по-настоящему, красный → FAIL. В `sync` прогон не делается (задержка на каждом ходе), его берёт на себя Stop-хук.

Что за ним остаётся — честно:

| Обход | Почему guard его не видит | Кто ловит |
|---|---|---|
| Косметическое касание тест-файла (пробел, комментарий) вместе с повышением | guard сравнивает факт изменения файла, не его смысл; пустая правка (только режим файла, `chmod`) не считается — она отброшена по `--numstat`, но пробел или комментарий засчитываются | adversarial verify + judge (I.3/I.3.5) |
| Переименование `id` claim'а в том же изменении, что и повышение | старого `id` в текущем файле больше нет, новый `id` не имеет базовой записи → «новый claim», а новые claim'ы не триггерят по конструкции. Самый дешёвый обход: правка одной строки, ни одного файла трогать не надо | adversarial verify + judge; сэмплирование на `audit` (переименованный claim выглядит как новый и попадает под authoring-требования SC-b) |
| Ослабление теста (снят assert, расширен допуск) вместе с повышением | тот же файл изменён — условие guard'а выполнено | adversarial verify + judge; assertion-density ловит только нулевой случай |
| Косметическая правка покрываемого кода (комментарий в файле из `path`) вместо усиления теста | guard видит факт изменения покрываемого файла, не его смысл | в `full` — обязательный зелёный прогон check'а (§20), дальше adversarial verify + judge |
| Повышение вместе с переписанной строкой `check` на другой, столь же слабый тест | `check` изменился → guard пропускает по конструкции (новый target валидирует `check_collectible`) | adversarial verify + judge |
| Нетронутый claim, сгнивший без единой правки | нет повышения — нечего сравнивать | случайное сэмплирование существующих claim'ов на `audit` (§4.2 `SKILL.md`) |

**Сужение WARN про долг (D33).** Открытый (`open`/`accepted`) долг на claim'е со `status: implemented` подсвечивается WARN'ом только при `severity: blocking`. `normal`/`cosmetic` долг на реализованном claim'е — норма (гигиена, улучшение поверх работающего кода), а не расхождение: WARN на каждой такой записи давал бы шум пропорционально длине списка долга (на прогоне — сотни записей) и обесценил бы остальные WARN'ы. Схема — `references/schema.md` §10.

SC-b без изменений: authoring-time доказательство «check был красным хотя бы раз», отревьюенное adversarial-проходом, остаётся обязательным — детерминированная проверка не подтверждает задним числом, что тест когда-либо ловил регрессию.

## 14. Status-raise guard — `check_status_raise` (D32)

Ранг статуса: `absent` = `design-only` = 0 < `stub` = 1 < `partial` = 2 < `implemented` = 3. Сравниваются только claim'ы с `kind: status`, по `id`, текущий `STATUS.yaml` против базовой версии.

**База сравнения:**

| Режим | База | Как получена |
|---|---|---|
| `sync` | `HEAD:<relpath>/STATUS.yaml` | `relpath` — от `git rev-parse --show-toplevel` (работает и в linked worktree, где `.git` — файл) |
| `full` | `GT_BASE_REF`, если задан; `HEAD~1` — только если `GT_BASE_REF` не задан вовсе. Заданный, но нерезолвящийся `GT_BASE_REF` → WARN, **без молчаливого отката на `HEAD~1`** (сравнение с произвольным коммитом дало бы произвольный вердикт) | `GT_BASE_REF` ставит CI: GitHub — `origin/<base_ref>` на PR и `github.event.before` на push; GitLab — `CI_MERGE_REQUEST_DIFF_BASE_SHA`, иначе `CI_COMMIT_BEFORE_SHA` |

**Вырожденные случаи (все — тихий skip или WARN, никогда ложный FAIL):**

| Случай | Поведение |
|---|---|
| `STATUS.yaml` не трекается git'ом (`sync`) | тихий skip — идёт `init`, базы ещё нет |
| В базовой версии нет `STATUS.yaml` по этому пути | тихий skip — первый коммит контракта |
| `GT_BASE_REF` не задан и `HEAD~1` не резолвится (первый коммит, shallow clone) | WARN `status-raise guard skipped: no base ref (set GT_BASE_REF)` |
| `GT_BASE_REF` задан, но не резолвится (all-zero sha от GitHub push на новую ветку, удалённый ref, shallow clone) | WARN `status-raise guard skipped: GT_BASE_REF '<ref>' does not resolve to a commit` |
| Базовый `STATUS.yaml` не парсится как YAML либо его `claims` — не список (`claims: null`, строка, mapping) | WARN `... does not parse`, проверка пропущена — непригодная база не приравнивается к «в базе не было claim'ов» |
| `git` недоступен / не git-репозиторий | `full` → **FAIL** `could not read baseline STATUS.yaml` (тот же принцип, что §3: не отработавший скан ≠ чистый скан); `sync` → WARN |

**Changed set** (что считается «тронуто в этом изменении»):

| Режим | Источники |
|---|---|
| `sync` | `git status --porcelain=v1 -z` (все статусы, включая untracked, обе стороны rename) + `git diff --name-only HEAD` |
| `full` | `git diff --name-only <base> HEAD` + тот же porcelain-набор, если рабочее дерево грязное |

Пути нормализуются в POSIX относительно toplevel, затем пересчитываются относительно `root` (если `root` ≠ toplevel); всё, что вне `root`, отбрасывается. Из changed set выбрасываются записи без содержательного диффа: если во всех просмотренных диффах путь идёт с `0 0` в `git diff --numstat` (изменение только режима файла, `chmod`), он не считается тронутым — байты, которые читает check, не менялись. Untracked-файлы в `--numstat` не появляются и потому никогда не выбрасываются; неразбираемые записи (`"`-quoted путь, rename-пара) остаются в changed set.

**Что считается target'ом check'а:**

| `check_kind` | Target |
|---|---|
| `pytest` | файл до `::` в каждой `;`-разделённой записи |
| `js_test` | файл до `::` |
| `go_test` | каталог до `::`; засчитывается изменение любого файла `*_test.go` под этим каталогом |
| `junit` | `fqn` до `::`; засчитывается изменение файла, чей путь оканчивается на `src/test/java/<fqn с "." -> "/">.java` |
| `probe` | путь до скрипта пробы |

**Условие FAIL — все пять разом:** ранг вырос; `check` посимвольно тот же; `check_kind` тот же; ни один target не в changed set; ни один файл из `path` claim'а не в changed set. Сообщение: `status raised <old> -> <new> but check target(s) <список> unchanged since <база>; strengthen the check in the same change`.

**Тронут покрываемый код — не FAIL, но в `full` check обязан быть зелёным.** `path` claim'а (строка или список; запись-каталог засчитывается по любому файлу под ней) сравнивается с тем же changed set. Попал — повышение объясняется новым поведением, а не одинокой правкой `STATUS.yaml`, и текстовый FAIL не выставляется. Взамен в `full` вызывается `run_claim_check` (§20):

| Исход прогона | Severity | Сообщение |
|---|---|---|
| check зелёный | — | ничего |
| check красный | **fail** | `status raised, check untouched and red: <хвост вывода>` |
| check не удалось запустить (нет тулчейна) | warn | `status raised, check not run: <причина>` |

В `sync` прогона нет: Stop-хук обязан оставаться субсекундным, а сам факт красного check'а он и так увидит на ближайшем `full`.

**Что НЕ триггерит:** понижение статуса; claim, которого не было в базовой версии; смена `kind` (например `live_state` → `status`); повышение вместе с изменённым `check`/`check_kind` (новый target проверяет `check_collectible`); повышение вместе с правкой файлов из `path` (см. выше).

Цена в `sync`: три коротких git-вызова (`rev-parse`, `status --porcelain`, `diff --name-only`) плюс `git show` одного файла — на порядок дешевле `git ls-files`-скана и `git log -L`, поэтому проверка живёт в базовом наборе, а не в `full`.

## 15. Разобранные примеры вывода

**Пример 1 — фантомный check (ловит `check_collectible`):**
```
$ python3 tools/ground_truth/verify.py --mode=sync
[FAIL] renewal_reminder_sends_before_expiry: check not collectible: test_sends_three_days_before_expiry not found in tests/test_renewal_reminder_service.py
```
Тест переименовали, `check:` в `STATUS.yaml` не обновили. Чинится правкой `check:` на актуальный node id (контракт разошёлся, не код).

**Пример 2 — orphan-код (ловит `check_coverage`):**
```
[FAIL] -: orphan code, no claim covers handlers/unsubscribe.py
```
Новый обработчик добавили, claim не завели — новый компонент вне контракта. Чинится добавлением claim'а (§5.4 в `references/schema.md`).

**Пример 3 — debt против правила resolved-while-stub (ловит `check_links`):**
```
[FAIL] homenet_health_needs_implementation: debt marked resolved but claim homenet_health_probe_wiring is still 'stub'
```
Кто-то пометил `debt.state: resolved`, не тронув `claims[...].status`. Чинится либо откатом `state` на `open`/`accepted`, либо реальным доведением claim'а до `implemented`/`design-only` — но не одной правкой `debt.state` без второй.

**Пример 4 — банворд (ловит `check_no_banned_words`, только `--mode=full`):**
```
[FAIL] -: services/notify.py:14: banned token 'Copilot'
```
Комментарий в коде упоминает происхождение куска логики. Чинится удалением упоминания либо, если это ложное срабатывание (например слово внутри строкового литерала теста на детектор), построчным `# gt-allow: <причина>`.

**Пример 5 — WARN, не FAIL (assertion density):**
```
[WARN] -: tests/test_delivery.py:22: test_sends_email has zero assertions
```
Не блокирует ни `sync`, ни `full` — сигнал, что конкретный тест не может ничего провалить. Не требует немедленного фикса, но обесценивает `check`, который на него ссылается — стоит доиграть на ближайшем `audit`.

**Пример 6 — повышение статуса без усиления check'а (ловит `check_status_raise`, оба режима):**
```
[FAIL] beta_worker_stub : status raised stub -> implemented but check target(s) tests/test_beta.py unchanged since HEAD; strengthen the check in the same change
```
Статус подняли, тест не тронули. Чинится либо усилением теста в том же изменении (тогда target попадает в changed set), либо возвратом статуса — но не одной правкой `STATUS.yaml`.

## 16. `coldstart_budget.py` — что входит в бандл, а что нет

`coldstart_budget.py FILE...` складывает байты явно перечисленных файлов (метрика SC-e, `SKILL.md` §5/§6) — список литеральный, скрипт ничего не обходит и не угадывает.

**Состав бандла «после» зафиксирован: `CLAUDE.md` репозитория + rules-файл `.claude/rules/ground-truth.md`.** Это то, что читается целиком на каждом холодном старте.

**Граница coldstart_budget.py.** Размер выбранных автоматических файлов — только размер этих файлов. Он не включает последующие поиски, STATUS, тесты и output, поэтому не является стоимостью задачи или доказательством экономии.

Сравнивать одинаковые наборы входов и одинаковые завершённые задачи. Байты не равны токенам; TOTAL/4 — грубая аннотация, не измерение. См. evaluation.md.

## 17. Свежесть аудита — `check_audit_due`

Возраст контракта и количество изменений — сигналы пересмотра. Сам возраст не доказывает, что утверждение неверно.

Два независимых признака «пора», оба про одно и то же — репозиторий уехал далеко:

| Признак | Откуда берётся | Порог |
|---|---|---|
| Календарный возраст | `meta.last_audited` (формат `YYYY-MM-DD`) | старше 30 дней (`_AUDIT_MAX_DAYS`) |
| Объём работы после аудита | `git diff --name-only <meta.last_audited_commit> HEAD`, отфильтрованный по `coverage.roots`/`coverage.exclude` тем же помощником, что использует `check_coverage` | больше 50 файлов (`_AUDIT_MAX_FILES`) |

Любой порог даёт audit due: WARN в sync и full. Дату меняют только после содержательного ревью, не ради зелёного CI.

**Почему два признака, а не один.** Календарь ловит контракт, который никто не открывал; счётчик файлов ловит месяц работы, сжатый в неделю, — по дате такой репозиторий ещё свежий, по факту в нём уже нечего узнать. Ни один из признаков не заменяет второй.

**Вырожденные случаи — только WARN, в обоих режимах:**

| Случай | Поведение |
|---|---|
| `meta.last_audited_commit` отсутствует | WARN `meta.last_audited_commit missing; the audit close step writes it` — репозитории, размеченные до появления поля, обязаны проходить дальше |
| `meta.last_audited_commit` не резолвится (перезаписанная история, shallow clone) | WARN `... does not resolve to a commit` |
| `git` недоступен | WARN `audit-due check skipped: <причина>` |
| `meta.last_audited` отсутствует или не в формате `YYYY-MM-DD` | признак не считается, находки нет (форму поля проверяет `check_shape`) |

`meta.last_audited_commit` пишет шаг закрытия `audit` — тем же коммитом, которым обновляет `meta.last_audited`. Формат — `^[0-9a-f]{7,40}$`, любое другое значение `check_shape` валит FAIL'ом сразу: тихо непонятая ссылка на коммит хуже отсутствующей, потому что выглядит как работающая.

## 18. Покрытие долгом — `check_debt_coverage`

Правило: **каждый `kind: status`-claim в состоянии `stub`, `partial` или `absent` обязан иметь запись в `debt` с `ref: <id>` и `state: open` или `accepted`.** Иначе — FAIL `status <status> without open/accepted debt -- add a debt entry with ref: <id>`.

Это зеркало давнего правила «`debt.state: resolved` невалиден при `stub`/`partial`/`absent`» (`references/schema.md` §10). То ловило закрытый долг на незакрытой работе, это — незакрытую работу без долга вообще. Без второй половины честная часть контракта ничего не стоит: claim может годами лежать в `stub`, и нигде не записано, что кто-то остался должен, — дыра читается как осознанное решение, а не как неоплаченный счёт.

`design-only` из правила исключён: это решение («здесь ничего не будет и не должно»), а не долг. `implemented` под правило не попадает по определению; открытый долг на нём — отдельный WARN, суженный до `severity: blocking` (D33, §13).

Проверка одинакова в обоих режимах — она чисто внутри `STATUS.yaml`, без git и субпроцессов.

## 19. Canary на каждый coverage-root — `check_canary_roots`

Раньше порог был «минимум один `canary: true` на репозиторий» (SC-b). В репозитории с пятью coverage-root'ами один canary доказывает, что зубы есть у check'ов под одним root'ом, и не говорит ничего про остальные четыре. Границу проводит сам контракт — `meta.coverage.roots`, — поэтому доказательство требуется на каждом.

Для каждого root'а считаются `kind: status`-claim'ы, чей `path` лежит под ним:

- есть хотя бы один claim в `implemented`/`partial` с валидным `check_kind`, и **ни на одном** claim'е под этим root'ом в статусе `implemented`/`partial` нет `canary: true` → `coverage root <root>: <N> implemented/partial claims, no canary -- add canary: true + mutation to one of them`. Canary обязан висеть на `implemented`/`partial`-claim'е: на `stub`/`absent`/`design-only` он root не закрывает — там нечего ломать, и про зубы check'ов реализованного кода такой canary не говорит ничего;
- **WARN в `sync`**, **FAIL в `full`**;
- root, где все claim'ы `stub`/`absent`/`design-only`, освобождён — ломать мутацией там нечего.

Мутационный протокол не меняется (§7): canary остаётся атрибутом существующего claim'а, а не отдельной записью, и теперь может висеть на `go_test`/`js_test`/`junit`/`probe`-claim'е — `run_claim_check` (§20) знает, как прогнать любой из них.

## 20. run_claim_check — реальное исполнение

`run_claim_check(root, claim, timeout=180) -> (ok, message, skipped)` —
общий исполнитель для full, guard и mutation selfcheck.

| Kind | Проверка результата |
|---|---|
| pytest | Один grouped запуск выбранных node-id, временный JUnit XML: непустой набор cases, без failure/error/skipped |
| go_test | go test -json -count=1 -run точного имени, из ближайшего go.mod; pass выбранного test, без runtime skip |
| js_test | Node TAP или Jest/Vitest JSON во временном файле; выбранные assertions должны выполниться и пройти. npx --no-install не скачивает отсутствующий runner |
| junit | Maven в найденном модуле и свежий Surefire XML; старый отчёт удаляется, отсутствие нужного testcase/ошибки/skip — не успех |
| probe | Реальный процесс: 0 — прошёл, 77 — не проверено, прочий nonzero — отказ |

Нулевой exit сам по себе недостаточен для тестового runner: он может означать
нулевую выборку или skip. Таймаут/ошибка запуска также не дают green.
Поведенческая сила теста всё равно требует содержательного ревью; XML не
доказывает связь assertion с каждым словом note.

В full pytest-claim'ы исполняются одним общим процессом, остальные checks
дедуплицируются по цели. Требуемый skip/missing toolchain даёт FAIL «не проверено»,
не утверждение «код сломан». Для доступного статического осмотра есть sync.
Отсутствующий pytest не считается гарантированно установленным только потому,
что установлен Python. Проектный venv и CI-зависимости обязательны.

GT_PYTEST_CHECK_ACTIVE предотвращает рекурсивный full из bridge-теста,
который сам выбран контрактом: такой bridge должен использовать sync.
Java smoke использует fake Maven; Node/Go/pytest выполняются локально.
JSON parser Jest/Vitest проверяется фикстурой отчёта, совместимость настоящей
версии runner дополнительно проверяется при установке в целевой проект.

## 21. Debt-resolve guard — `check_debt_resolve`

Правило: **долг, закрытый в этом изменении, обязан оставить след в коде.** Берутся записи `debt`, чей `state` сейчас `resolved`, а в базовой версии был `open`/`accepted` — или долга там не было вовсе, что «тоже подозрительно» и идёт через ту же проверку: свежепойманный `ref`, указывающий на нетронутые файлы, закрыт от рождения, а не доказан. Дальше — по claim'у из `ref`: если он был в базовой версии, требуется, чтобы `check` или `check_kind` посимвольно изменились, либо чтобы хоть один target claim'а (`_check_targets`, та же таблица, что в §14) попал в changed set (`_changed_paths`, тот же helper, что использует `check_status_raise`); если базового claim'а не было, второй критерий (targets) остаётся, а сравнивать `check`/`check_kind` не с чем. Если у `ref`-claim'а нет `check` (обычный случай для `live_state`/`out_of_repo`), targets вместо этого берутся из его `path` (список, та же нормализация путей); нет ни `check`, ни `path` — вместо FAIL выдаётся `warn` в обоих режимах: `debt <id> resolved without proof: claim <ref> has no check or path to verify against; fix: name the commit or test in description`. Ничего из этого не случилось → FAIL в обоих режимах: `debt <id> resolved without proof: claim <ref> check unchanged and targets untouched since <база>; fix: change the check/test that proves it, or keep state: open`.

**FAIL в обоих режимах, не WARN в `sync`.** Это тот же принцип, что у `check_status_raise` (§14): закрытый без доказательства долг — не протухшая проза, а ложь по факту прямо в этом изменении, и Stop-хук обязан её ловить немедленно, а не откладывать до `full`.

**База сравнения и вырожденные случаи** — те же, что в §14 (таблица там же): `HEAD` в `sync`, `GT_BASE_REF`/`HEAD~1` в `full`; недоступный git → `full` FAIL / `sync` WARN `could not read baseline STATUS.yaml: <причина>` (тот же текст, что у `check_status_raise`, через общий `GitUnavailable`); если базовая версия не резолвится вовсе (первый коммит, `_baseline_claims` вернул `None` без варианта WARN) — проверка тихо пропускается.

**Что НЕ триггерит:** долг, который был `resolved` уже в базовой версии (закрыт раньше, не в этом изменении); `ref`, не указывающий ни на один текущий claim (это забота `check_links`, §2); `check`/`check_kind`, изменившиеся вместе с нетронутыми target-файлами — довольно любого из двух признаков; `debt.state`, отличный от `resolved` (открытый/принятый долг — не предмет этой проверки, см. `check_debt_coverage`, §18).

**Второй уровень — фикс лёг в более раннюю, уже слитую правку.** Прежде чем дойти до FAIL, при наличии `debt.opened` в формате `YYYY-MM-DD` проверяется `git log -1 --since=<opened> -- <targets>` за всю историю, а не только за диапазон база..worktree. Нашёлся коммит — `warn` в обоих режимах: `debt <id> resolved without a change in this range; targets last touched <sha> <дата>, after the debt was opened; fix: name that commit or test in description`. Не нашёлся (или `opened` нет/битый формат) — прежнее поведение, FAIL.

## 22. Content-anchored цитаты — `check_cited_lines`

Правило: **если прозаическое поле claim'а цитирует конкретную строку кода, цитата обязана совпадать с тем, что там реально написано.** Ищутся только строковые поля `note` у claim'ов — это единственное поле в схеме (`references/schema.md`), где встречаются цитаты кода; `why` документирован под ссылки на артефакты аудита (`"audit-2026-09-04#3"`), а не под `path:N`, поэтому не сканируется.

**Что считается цитатой:** `path:N`, где `path` содержит `/` или точечное расширение (иначе `N` слишком легко спутать с версией или номером порта), а `N` — номер строки; и в пределах 120 символов ПОСЛЕ этого совпадения — кавычки `«…»`, `"…"` или `` `…` `` с непустым содержимым. Пара «путь:строка + цитата рядом» и есть content anchor; голый `path:N` без цитаты — не по этому адресу (см. ниже). Окно поиска обрезается по первому `;` после цитирования (заметки используют `;` как разделитель клауз, см. note-hygiene) — цитата, оказавшаяся за этой границей, просто не проверяется.

**Проверка:** файл существует, строка `N` (1-based) есть в файле, и текст цитаты (после `.strip()`) входит подстрокой в `.strip()` этой строки. Не выполнено → `warn` в `sync`, `fail` в `full`: `claim <id>: cited line <path>:<N> does not contain «<цитата, обрезана до 60 символов>»; fix: run tools/ground_truth/remap_line_refs.py --apply or update the note`.

**WARN в `sync`, не FAIL.** В отличие от §21, разъехавшаяся цитата — это протухшая проза (код сдвинулся, номер строки устарел), а не ложь, вписанная в это же изменение; тот же класс, что протухший `path` (§13) — Stop-хук предупреждает и даёт команду чинить, `full` требует починки.

**Диапазон `path:A-B`:** цитата ищется построчно на всём `A..B` (`B` ограничен длиной файла) — достаточно попасть в любую из строк; `B < A` откатывается к одиночной `path:A`, как раньше.

**Что этот check НЕ ловит:** `path:N` без цитаты рядом (нечего сверять — это адрес для `templates/remap_line_refs.py`, который двигает номер при сдвиге кода, но не проверяет содержимое); цитату без валидного `path:N` рядом; несуществующий файл (уже покрыт `check_coverage`/`check_shape`, §2); несколько путей в одном совпадении — берётся первый `path:N`/`path:N-M` буквально; поля `why`/`description`/`reason`/`evidence` — не сканируются, потому что в схеме их либо нет, либо они не для цитат кода; отступ/форматирование строки — сравнение идёт по `.strip()` обеих сторон, лишние пробелы вокруг цитаты не считаются расхождением.
