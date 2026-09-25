> Полноценный первичный init обязателен по SKILL.md и init.md. I.1–I.11 ниже — переиспользуемые механизмы исследования и доказательств, а не вся современная приёмка: дополнительно обязательны model.yaml, канонические области, receipts, task packets и тесты дрейфа. Прежние модели, лимиты чтений, исторические разрешения, автоматические уведомления/cron и обязательный флот на обычный sync не действуют. Использовать доступную оркестрацию в пределах запроса. Номера старого SKILL.md относятся к full-init.md.

# pipeline.md — раннбук по скриптованному прогону I.1–I.11

Отвечает на вопрос «что запустить сейчас и чего от этого ждать»: одна секция на шаг цепочки I.1→I.11, включая пост-I.5 цепочку сверки дрейфа строк и raise-vote, установщик I.9 и приёмку I.10. На каждый шаг — точная команда или вызов Workflow, вход, выход, ETA-базис по последнему прогону (репозиторий `service-a`; журнал того прогона не опубликован), точки, где решение за владельцем, и напоминание про Monitor-сторож. Имена стадий/workflow здесь совпадают с `pipeline/SPEC.md` — SPEC.md обязателен к прочтению, если имя стадии здесь разойдётся с файлом на диске, прав SPEC.md, не этот файл.

**ETA-базис — не обещание.** Все минуты ниже измерены один раз, на одном репозитории (`service-a`, репозиторий среднего размера на нескольких языках). Другой репозиторий даст другие числа; D38 требует называть ETA владельцу перед каждой фазой, но как ориентир, не гарантию.

## 0. Конвенции

- **`run.json`** — шесть строковых полей (`repo, research, scratch, python, skill, date`), см. `pipeline/run.example.json` и `pipeline/gt_lib/paths.py::load_run`. Один файл на прогон, путь к нему передаётся каждой стадии и в args-файл каждого Workflow.
- **Питоновская стадия:** `python3 <skill>/pipeline/gt.py <stage> --run run.json [флаги стадии]`. `<skill>` — значение `run.json.skill`. `gt.py --list` печатает имя + первую строку докстроки каждой стадии, `gt.py --help` — usage.
- **Workflow-шаг:** main вызывает Workflow tool с `scriptPath = <skill>/pipeline/workflows/<имя>.js` и `args` = содержимое args-файла, который строит предыдущая питоновская стадия (`run.scratch/<phase>/<phase>-args.json`, кроме recon — там `run.scratch/recon/recon-args.json`, и vote — `run.scratch/vote/vote-args.json`). Каждый workflow возвращает один объект: исходные ключи фазы без изменений + `ok`, `counts`, `failed` (полная форма — `pipeline/SPEC.md` §1.4). Инструмент заворачивает его в `{"result": {...}}` в `<tasks>/<taskId>.output`; тот же результат лежит в `journal.jsonl` строкой `{"type":"result","result":{...}}` — читающая стадия (`i35_dump_judged.py`, `i4_apply.py` и т.д.) принимает оба источника, `--journal PATH` или `--session-dir DIR --workflow-id ID`.
- **Модели по ролям, не по фазам.** `--models FILE` у стадии, строящей args-файл, копирует объект в поле `models` args-файла (флаг не передан → поле отсутствует, workflow берёт дефолты из своего `DEFAULTS`). Полная таблица роль→дефолт — `pipeline/SPEC.md` §1.3 и `SKILL.md` §2.
- **Куда что пишется.** Артефакты, нужные следующей фазе и Outcome, — в `run.research/` (`inventory.json`, `claims.json`, `codemap-*.json`, `debt-candidates.json`, `slices/`, `claims-judged.json`, `i4-*.json`, `i5/`, `i6/`, `raise-candidates.json`). Всё остальное (args-файлы, батчи, отчёты, бэкапы `STATUS.yaml`) — в `run.scratch/<phase>/`. Внутрь репозитория пишут только `i4_apply`, `i5_recon_apply`, `i5_revert_raises`, `i9_install`, и только с флагом `--write` (по умолчанию — сухой прогон/план).
- **Сторож — три канала (D40 + D48).** Любой запуск Workflow или фонового агента длиннее ~10 минут сопровождается: (1) persistent `Monitor` (`persistent: true`, не Bash-цикл — обрыв одной нотификации иначе гасит цепочку насовсем) на `<tasks>/<taskId>.output`: `DONE` по непустому файлу, `QUOTA` по новым строкам «hit your … limit» в `journal.jsonl`, `STALL` если journal не получал новый `result` 30 минут, `PROGRESS` раз в 15 минут; (2) cron-heartbeat в сессии (`CronCreate`, recurring, минуты `3-59/10`, prompt — самопроверка output/journal и продолжение конвейера либо одна строка «без изменений»; session-only, 7 дней, переармировать первым ходом новой сессии, снимать `CronDelete` при закрытии прогона); (3) `PushNotification` владельцу на границах фаз и при `QUOTA`/`STALL`. Квоту планировщик не лечит: при `QUOTA` — `TaskStop`, запись в plan.md, push; resume тем же run id после reset. Main не ждёт нотификацию как единственный канал. Ниже это отмечено у каждой фазы, где последний прогон реально перевалил за 10 минут.
- **Точки решения владельца** помечены явно в тексте фазы; всё остальное main решает и исполняет сам.

## I.1 — Research sweep

- Workflow: `scriptPath = <skill>/pipeline/workflows/i1-research.js`, `args = {repo, research, roots, models?}` (`roots` — список областей покрытия `[{name, path, language}]`, задаёт main; по одному codemap-finder на элемент).
- После: `python3 <skill>/pipeline/gt.py i1_check_research --run run.json --journal <transcript dir>/journal.jsonl [--strict]` — сначала перезаписывает четыре файла в `run.research` из structured-результатов финдеров в журнале (валидированный payload; файл, который агент пишет сам, не валидирован — на fixture один финдер файл не записал, другой положил не тот верхний ключ; путь к transcript dir печатает сам Workflow tool при запуске), затем сверяет `inventory.json`/`claims.json`/`codemap-*.json`/`debt-candidates.json` против `pipeline/schemas/*.schema.json`, печатает таблицу счётчиков, падает на первом расхождении типов.
- Вход: репозиторий как есть (read-only), список coverage-областей от main.
- Выход: `run.research/{inventory,claims,codemap-*,debt-candidates}.json`.
- ETA-базис: 43 мин (4 финдера параллельно). Monitor обязателен.
- Владелец: не вовлечён на этом шаге.

## I.2 — Слайсы по компонентам

- `python3 <skill>/pipeline/gt.py i2_build_slices --run run.json [--dry-run]` — режет `claims.json`/`debt-candidates.json`/`codemap-*.json` на `run.research/slices/<component>.json` + `run.research/slices-index.json`.
- `python3 <skill>/pipeline/gt.py i2_build_args --run run.json [--models FILE]` — пишет `run.scratch/i2/i2-args.json`, печатает число слайсов/claim'ов для прикидки ETA следующей фазы.
- Вход: результаты I.1.
- Выход: `run.research/slices/*.json`, `slices-index.json`, `run.scratch/i2/i2-args.json`.
- ETA-базис: секунды (детерминированный скрипт, не агент).
- Владелец: не вовлечён.

## I.2 – I.3.5 — Reconcile / Adversarial / Judge

- Workflow: `scriptPath = <skill>/pipeline/workflows/i2-i35.js`, `args` = `run.scratch/i2/i2-args.json`. Три фазы одного вызова: Reconcile (один drafter на компонент), Adversarial (defender над всеми черновиками компонента, prosecutor только на claim'ы, повышающие уверенность или требующие code-fix — 3-vote асимметричен по D12), Judge (батчи 5–8).
- После: `python3 <skill>/pipeline/gt.py i35_dump_judged --run run.json (--journal PATH | --session-dir DIR --workflow-id ID) [--task-output PATH]` — `--task-output` предпочтителен: journal-фолбэк восстанавливает items из reconcile-результатов без голосов defender/prosecutor — собирает вердикты judge в `run.research/claims-judged.json`, печатает отчёт по маршрутам/статусам.
- Затем: `python3 <skill>/pipeline/gt.py i35_assemble_status --run run.json --repo-name NAME --roots a,b,c [--runner pytest|go|js|none] [--out-dir DIR] [--audited YYYY-MM-DD]` — собирает черновик `STATUS.yaml` + `escalate.json` + `code-fix-candidates.json` + `canary-candidates.json` + `assemble-report.json` в `--out-dir` (по умолчанию `run.scratch/i35/`). Несуществующий `final_path` (judge назвал отсутствующий файл absent-claim'а) в черновик не попадает: подставляется первый существующий файл из цитат judge'а (сначала `debt.path`/description, затем `final_note`), иначе ближайший существующий каталог; дефект `path_missing` в `assemble-report.json` несёт `substituted` (`null` — якоря нет, чинить judged-запись руками до копирования в репо).
- Выход: `run.research/claims-judged.json`, черновик `STATUS.yaml` в scratch (владелец/main копирует одобренный черновик в репозиторий — сама стадия внутрь репо не пишет).
- ETA-базис: около 2 ч 45 мин суммарно на весь workflow (на `service-a` сюда попал упор в лимит сессии и возобновление — заложить запас). Monitor обязателен.
- Владелец: не вовлечён на самом прогоне; читает `escalate.json`, если judge не смог разрешить спор prosecutor/defender.

## I.4 — Code-fix (только self-inconsistency, D12)

- `python3 <skill>/pipeline/gt.py i4_classify_briefs --run run.json` — детерминированная часть: `run.research/i4-triage.json` (недостающие pytest node-id, файлы под пробы), `run.research/i4-groups.json`; список брифов для классификатора — `run.scratch/i4/classify-briefs.json`. Метки класса (`test_only`/`probe_only`/`mixed`/`prod_change`/`self_inconsistency`/`prod_files`) сама стадия не присваивает — это первая фаза `i4.js` (роль `codefix`); метка класса никогда не входит во вход брифа — D12 запрещает правку прод-кода в I.4 независимо от класса.
- `python3 <skill>/pipeline/gt.py i4_build_args --run run.json [--canaries N] [--models FILE] [--i35-dir DIR]` — по `claims-judged.json` + `i4-triage.json` + `i4-groups.json` + `canary-candidates.json` (id канареек берутся только из этого файла, не из литерального списка) пишет брифы в `run.research/i4/{probe,test,canary}/*.json` и `run.scratch/i4/i4-args.json`. `--i35-dir` — где искать `canary-candidates.json`, по умолчанию `run.scratch/i35` (совпадает с дефолтным `--out-dir` шага `i35_assemble_status` выше — сменил там, укажи здесь тем же значением).
- Workflow: `scriptPath = <skill>/pipeline/workflows/i4.js`, `args = run.scratch/i4/i4-args.json`. Фазы Classify/Probes/Tests/Canaries, всё на `M.codefix`; пробы гоняются через `args.python` с `PYTHONDONTWRITEBYTECODE=1 -p no:cacheprovider`, red-демо только в копии под `tmp_base`, никаких правок вне брифа и вне прод-кода.
- После: `python3 <skill>/pipeline/gt.py i4_apply --run run.json (--journal PATH | --session-dir DIR --workflow-id ID) [--nodes FILE] [--head-override FILE] [--write] [--dry-run]` — заносит результаты в `run.research/claims-judged.json` (только с `--write`), пишет `run.research/i4-results.json` и `run.research/i4-brief-classes.json`. Сначала `--dry-run`: `node_id` из отчёта тест-агента резолвится по AST файла как написан — флаг `node_resolved` (агент отчитался голым id, тест лежит в классе) безвреден, `node_not_in_worktree` — id отброшен, смотреть отчёт агента.
- ETA-базис: 69 мин. Monitor обязателен.
- Владелец: не вовлечён напрямую; список кандидатов на «дореализовать заглушку», которых I.4 намеренно не тронул (D12), уходит в Outcome (I.11) на решение владельца.

## I.5 — Doc reconciliation + карантин

- `python3 <skill>/pipeline/gt.py i5_build_args --run run.json [--status PATH] [--models FILE]` — доки = `git ls-files '*.md'` минус `docs/_human/**`, области = coverage roots из `meta` черновика `STATUS.yaml`; пишет `run.scratch/i5/i5-args.json`, печатает число документов и предупреждает при >60 (D39 — review-фаза выносится во второй Workflow, очередь в одном workflow — cap 8).
- Workflow: `scriptPath = <skill>/pipeline/workflows/i5.js`, `args = run.scratch/i5/i5-args.json`. Два параллельных конвейера: Docs (quarantine-classifier → editor → reviewer → fixer, максимум 2 раунда FAIL, дальше override main) и Comments (comment-editor → guard → reviewer → fixer). Бриф editor'а несёт список claim'ов/debt, ссылающихся на документ, агент возвращает `citing_entries_touched: [{id, old_anchor, new_anchor|stale}]` — фаза I.5 не закрыта, пока этот список не применён.
- `python3 <skill>/pipeline/gt.py i5_comment_guard --run run.json [--file PATH ...]` — по умолчанию по всем модифицированным трекаемым файлам; сравнивает HEAD и рабочее дерево с вырезанными комментариями/докстрингами (AST для Python, YAML с opaque-тегами, generic line-comment fallback); `OK`/`CHANGED-STRUCTURE`/`UNSUPPORTED` на файл, exit 1 при любом не-`OK`.
- **Владелец, точка решения:** список удалений и перемещений в `docs/_human/` утверждается ДО действия — main не выполняет `git mv`/`git rm` по своей инициативе.
- ETA-базис: около 4 ч по часам, включая паузу на rate-limit (429) внутри окна — считать основную нагрузку ближе к 1–1.5 ч агентского времени, остальное — ожидание лимита. Monitor обязателен, включая срабатывание на паузе (STALL корректно сигналит паузу, не тупик).

### Пост-I.5 — цепочка сверки дрейфа строк

Правки I.4/I.5 сдвигают номера строк, на которые ссылаются claim'ы и debt. Этот блок обязателен между I.5 и I.6/I.9, не опционален.

- `templates/remap_line_refs.py [--base HEAD] [--apply] [--dry-run] [--pre PATH] [--report PATH] STATUS_FILE` — переписывает `path:N`, `path:N-M`, списки через запятую/`and`, оторванные `:N` и прозу («line N» / «lines N-M and P-Q») с нумерации базового коммита на нумерацию рабочего дерева. Библиотечное API (`parse_diff`, `build_mapper`, `process_value`, `process_status_file`) остаётся импортируемым — этим же модулем пользуется `i5_recon_apply`, копии regex-а не держит. Отчёт (`--report x.json`) несёт `rewritten`/`deleted`/`unparsed`/`assoc_risk`: предложение, называющее два и больше изменённых файла с оторванной или прозаической ссылкой, только репортится, никогда не переписывается молча. По умолчанию dry-run; `--apply` переписывает атомарно и перевалидирует `yaml.safe_load`. `--pre PATH` — копия `STATUS.yaml` до любого remap против этой базы (снять `cp` перед первым запуском): без неё прозаические ссылки («line N») только репортятся как `unparsed`, с ней — переписываются, когда окно текста вокруг ссылки байт-в-байт совпадает с копией. Базовый снапшот берётся ДО I.4 (не только до I.5) — иначе строки цитируются против нумерации, которую I.4 уже сдвинула.
- `python3 <skill>/pipeline/gt.py i5_recon_build --run run.json [--status PATH] [--pre PATH] [--report PATH] [--pass 1|2] [--unbatched FILE] [--models FILE]` — отбирает записи `STATUS.yaml`, чьи якоря (`path:N`, диапазоны, оторванные `:N`, проза) пересекаются с ханками `git diff HEAD`, не только те, что переписал remap. Флаги `low_sim`/`deleted`/`substantive`/`assoc_risk`/`prose_far`/`replaced`/`unparsed`/`quoted_removed` (последний — п.37: `note`/`description` повторяет ≥5 подряд слов строки, удалённой в `git diff HEAD`; якорь не нужен, в cite лежит удалённый текст). Пишет батчи `run.scratch/recon/batch-NN.json` (второй проход — `--pass 2`, в `run.scratch/recon/pass2/`) и `run.scratch/recon/recon-args.json`.
- Workflow: `scriptPath = <skill>/pipeline/workflows/recon-review.js`, `args = run.scratch/recon/recon-args.json`. Фаза Review (`M.reviewer`, один батч на агента) → Judge (`M.prosecutor`, рефутирует каждое actionable-предложение). Результат `{results:[{batch, proposals, verdicts}], missing, ok, counts, failed}`.
- `python3 <skill>/pipeline/gt.py i5_recon_apply --run run.json --results FILE [--skip-ids FILE] [--status PATH] [--pass 1|2] [--write] [--dry-run]` — сначала разворачивает голые basename в `new_text`/`corrected_text` до полного пути (однозначные — разворачивает, неоднозначные — только репортит), затем применяет по вердикту (`rewrite` с валидацией якоря, `resolve`, `remove`; `resolve` на debt, чей `ref`-claim не implemented, отклоняется с логом — разрыв в коде правкой доки не закрывается, reviewer переписывает описание). `raise_candidate`-предложения уходят в `run.research/raise-candidates.json` и сами по себе никогда не применяются. Бэкап `STATUS.yaml` — `run.scratch/i5/STATUS.pre-recon<pass>.yaml`, где `<pass>` = значение `--pass` (по умолчанию 1).
- Второй проход — тот же `i5_recon_build --pass 2` + `recon-review.js` + `i5_recon_apply --pass 2` над оставшимся хвостом. `--pass` обязателен на втором проходе: без него `i5_recon_apply` по умолчанию пишет `STATUS.pre-recon1.yaml` и затирает бэкап первого прохода — единственную копию `STATUS.yaml` до recon.
- ETA-базис: проход 1 — 40 мин, проход 2 — 17 мин (16.5 мин на `service-a`, 5 батчей/10 агентов). Monitor на первом проходе обязателен, на втором — по факту (граница 10 мин).

### Raise-vote — рефутация повышений статуса

- `python3 <skill>/pipeline/gt.py i5_vote_args --run run.json [--candidates FILE] [--status PATH] [--models FILE]` — по `run.research/raise-candidates.json` (переопределяется `--candidates`) резолвит `check`/`docs`/`code` каждого claim'а из `STATUS.yaml` (переопределяется `--status`, по умолчанию `run.repo/STATUS.yaml`), пишет `run.scratch/vote/vote-args.json`.
- Workflow: `scriptPath = <skill>/pipeline/workflows/raise-vote.js`, `args = run.scratch/vote/vote-args.json`. Фаза Refute: три независимых рефутера на claim (`M.prosecutor`), read-only бриф, `STATUS.yaml` читается точечно grep'ом по id, не целиком. Результат `{votes:[{id, verdicts}], ok, counts, failed}`.
- `python3 <skill>/pipeline/gt.py i5_revert_raises --run run.json --votes FILE --backup FILE [--status PATH] [--write] [--dry-run]` — для каждого claim'а с большинством `refuted` откатывает `status`/`check`/`note` из бэкапа `STATUS.yaml` через `gt_lib.yaml_edit`, повторно открывает долг, который повышение статуса закрывало.
- ETA-базис: отдельно не измерялся на `service-a` (шёл внутри окна recon); голосование по одному батчу claim'ов — короче recon-прохода, Monitor по факту.
- Владелец: не вовлечён — рефутация чисто механическая проверка доказательств, откат применяется автоматически по большинству голосов.

## I.6 — Mapping-audit

- `python3 <skill>/pipeline/gt.py i6_build_args --run run.json [--n 18] [--seed N] [--models FILE]` — детерминированная выборка N проверяемых утверждений из до-правочного текста доков (`git show HEAD:<doc>`), пишет `run.research/i6/sample.json` и `run.scratch/i6/i6-args.json`.
- Workflow: `scriptPath = <skill>/pipeline/workflows/i6.js`, `args = run.scratch/i6/i6-args.json`. Фаза Audit: аудитор A по фиксированной выборке, аудитор B — самостоятельная выборка 15–20 утверждений из до-правочных доков; оба `M.audit`, вердикты `keep-inline`/`moved-to`/`link`/`drop+причина`, default-FAIL при сомнении.
- ETA-базис: около 20 мин. Monitor обязателен.
- Владелец: не вовлечён; итог (сколько утверждений потеряно без причины) уходит в Outcome.

## I.7 — doc-model (опционально)

Не скриптовано — вызов существующего скилла `doc-model` напрямую, только после того, как контракт установил правду (I.1–I.6 закрыты). См. `references/doc-model-bridge.md`. ETA и Monitor — по правилам самого `doc-model`, здесь не нормируются.

## I.8 — Navigation layer

Не скриптовано — main вызывает одного opus-агента (effort medium) напрямую (Agent tool), пишущего `.claude/rules/ground-truth.md` и роутинг-таблицу; нет отдельной стадии или workflow, потому что вход — не пакет claim'ов, а весь установленный контракт целиком. ETA на `service-a` не выделялась отдельно от подготовки I.9.

## I.9 — Verifier + hook + CI wiring (установщик)

- `python3 <skill>/pipeline/gt.py i9_install --run run.json [--facts] [--dry-run] [--write] [--pkg PATH] [--js-test-dir PATH] [--java-pkg DOTTED] [--ci-file PATH]`. `--facts` собирает и печатает факты репозитория (трекается ли `.claude/`, какой CI-файл есть, какой pytest/go/js/maven раннер стоит — для Maven это `pom.xml` + `src/test/java` и самый мелкий тестовый пакет, есть ли `.venv/bin/python`) и выходит без записи. Без флагов — `--dry-run` печатает план. `--write` раскладывает `templates/*` по репозиторию (полная таблица источник→назначение — `SKILL.md` §5), мёржит `Stop`/`SessionStart`/`PreToolUse` записи в `settings.json`/`settings.local.json` по строке команды (не заменяя массив целиком); у уже присутствующего хука `timeout` поднимается до шаблонного, вниз не трогается, добавляет CI job в существующий файл или создаёт новый. `--ci-file PATH` — repo-relative workflow-файл (или `.gitlab-ci.yml`), который получает job, если авто-детект находит в репозитории больше одного workflow. `--java-pkg DOTTED` — пакет для JUnit-моста, если авто-детект пакета не сработал (тесты в default package) или нужен другой. Идемпотентна: второй запуск не даёт диффа.
- Лессон п.12: сам верификатор, который эта стадия ставит, гоняет исполняемые `.py`-пробы через `sys.executable`, не через shebang (иначе попадает системный python вместо venv), а JS-тест без `test`/`it`/`describe(` резолвится fallback'ом на поиск quoted-литерала имени теста.
- Лессон п.15: если после установки `verify.py --mode=full` валит FAIL на настоящем слове-триггере бан-листа в продовом файле (не в тестовой фикстуре, не в самом контракте) — штатный ход `meta.banned_words.exclude: [{path, why}]` в `STATUS.yaml`, а не подавление или ослабление проверки.
- Лессон п.85 (D61): CI job доказывается в CI-образе репозитория ДО push, не на маке: `docker run --rm -v <основной клон>:<тот же абсолютный путь>:ro -v job.sh:/job.sh:ro <image> sh /job.sh`, где `job.sh` = `before_script` + `script` job'а дословно поверх `git clone -q <worktree> /repo && cd /repo` (клон берёт только HEAD — сначала коммит) и `export GT_BASE_REF=$(git merge-base origin/main HEAD)`, плюс `export` каждой глобальной `variables:` CI-файла (`service-a`: `ANSIBLE_HOST_KEY_CHECKING` — без неё docker-прогон был зелёным, а раннер красным); в конце — `verify rc=`/`selfcheck rc=`/`guard rc=`. Зелёный локально ≠ зелёный в образе: slim/alpine не несут git/curl/ripgrep, `ansible-core` без коллекций ≠ `ansible`, `.venv` в образе нет (`service-a`: 10 FAIL ansible-проб и «role not found» в первом реальном прогоне, `service-c`: 40 FAIL банвордов из pip-кэша).
- Лессон п.86 (D61): job наследует setup тестового job'а репо — anchor `before_script` (`*install-base`), placeholder-файлы (`.vault_pass`), `chmod 755 .` под world-writable build dir раннера — и стоит с `needs: []`, иначе чужой красный lint (`service-c`: ruff) прячет результат контракта. Шаблон `ci-gitlab-addition.yml` несёт оба пункта комментариями внутри job'а (они переживают установку, header-комментарии — нет).
- Лессон п.87 (D61): `cache:`-path внутри checkout (`$CI_PROJECT_DIR/.pip-cache`) обязан быть в `.gitignore` — `check_no_banned_words` читает untracked-файлы (D36), а метаданные сторонних пакетов полны стоп-слов. `i9_install` считает это фактом (`ci_cache_paths` / `ci_cache_unignored`, gitlab `cache.paths` и github `actions/cache` `path:`) и дописывает `.gitignore` на `--write`; docker-прогон п.85 этого класса не ловит (клон без кэша) — потому проверка детерминированная, не ручная.
- Лессон п.88 (D61): проба, зовущая внешний бинарь (curl, sha256sum, ansible, ssh), — `shutil.which` → exit 77 либо stub на PATH, когда скрипт под тестом лишь проверяет наличие бинаря до проверяемой ветки (`service-d`: stub `curl` в guard-кейсах `install.sh`); проба, собирающая все tracked тест-файлы, требует в job import-зависимости каждого из них, не только прогоняемой сьюты (`service-a`: `roles/<role>/files/requirements.txt` ради одного `aiohttp`-теста). Проба, меряющая origin настройки (`ansible-config dump`), чистит ambient env от `ANSIBLE_*` — глобальные `variables:` раннера иначе переводят origin в env, и claim о файле ложно краснеет (`service-a`: `<probe>.py`).
- Выход: `tools/ground_truth/*`, hooks, CI job, `.claude/rules/ground-truth.md` (если ещё не установлен I.8), `.claude/settings.json`/`.local.json`.
- ETA-базис: отдельно на `service-a` не хронометрировалась (детерминированный скрипт, без агентов) — ожидать единицы минут на установку плюс время самого `verify.py --mode=full`.
- Владелец: не вовлечён, если только I.9 не находит два конфликтующих CI-файла или неоднозначный test runner — тогда main спрашивает перед записью (разрешается флагом `--ci-file`, который называет, какой файл получает job).

## I.10 — Acceptance: mutation-selfcheck → fleet-probe

- `python3 <repo>/tools/ground_truth/gt_mutation_selfcheck.py [--allow-dirty]` — прогоняется main напрямую (это установленный шаблон, не стадия `gt.py`), строго ДО навигаторов, не параллельно: мутация на секунды меняет файл, который навигатор может в это время читать. Лессон п.19: `--allow-dirty` обязателен, когда I.10 идёт до коммита в грязном рабочем дереве (I.4/I.5 трогали те же файлы, что и канарейки) — восстановление всегда идёт из сохранённых в память байт, флаг только снимает git-проверку чистоты. Без флага — строгий режим, тот же скрипт вызывается Stop-хуком/CI.
- `python3 <skill>/pipeline/gt.py i10_build_args --run run.json [--seed N] [--n 5] [--status PATH] [--models FILE]` — детерминированно (под `--seed`) выбирает цели фрейт-проб из `STATUS.yaml`, вразброс по coverage roots, предпочитая файлы с двумя и более claim'ами; пишет `run.scratch/i10/i10-args.json`.
- Workflow: `scriptPath = <skill>/pipeline/workflows/i10-fleet.js`, `args = run.scratch/i10/i10-args.json`. Фаза Acceptance: свежие навигаторы (`M.navigator`) отвечают «куда полез бы менять X» за ≤3 чтения claim-id'ом, затем один грейдер (`M.grader`) сверяет каждый ответ с фактическим прогоном `blast_radius.py` и `STATUS.yaml`, не с самоотчётом навигатора.
- ETA-базис (лессон п.20): ~5 мин на раунд, не ~25 — старая оценка D38 была получена как затраты на несколько повторных раундов, а не на один; фактический прогон на `service-a` — 3 м 45 с на 5 навигаторов + opus/high грейдер за один раунд. 25 мин закладывать только если ожидается несколько раундов (провал и повтор). Monitor не обязателен при ожидаемых <10 мин, но полезен на первом раунде нового репозитория, пока фактическое время не известно.
- Владелец: не вовлечён, если раунд проходит 5/5 (SC-d); при провале main решает, повторять раунд или эскалировать.

## audit — детерминированная проверка и адресное ревью

Один run.json: repo/research/scratch/python/skill/date (см. pipeline/run.example.json).
audit_scope --run run.json [--max-age-days 30] [--sample 5] [--seed N]
сравнивает с meta.last_audited_commit, а не последним sync STATUS.yaml.
Учитывает dirty/untracked-код, цели checks, изменённые/удалённые claims и coverage.
Без --seed выборка воспроизводима по HEAD и run.date. --skip-verify никогда
не даёт SHORT-CIRCUIT. Выход: scratch/audit/audit-scope.json, без записи в репо.

SHORT-CIRCUIT — выполненные проверки и актуальная неизменившаяся область:
достаточно отчёта. Никаких обязательных I.10, fleet или audit_close.
SCOPED — адресное исследование drift/touched/sample; не автоматические I.1–I.6.
Навигационный spot-check нужен при изменении маршрутизации. Template drift —
сигнал посмотреть dry-run/diff i9_install, не разрешение перезаписать инструменты.

audit_close --run run.json --reviewed --write обновляет дату и commit только
после содержательного ревью. Заново запускает full текущего репозитория и
проверяет HEAD scope. --force позволяет первый audit без scope, но не обходит
fresh verify. Без --write показывает diff строк. Audit-отчёт сам по себе
не разрешает изменения/коммиты. Подробный текущий контракт — process.md.

## I.11 — Outcome

Не скриптовано — main лично собирает `## Outcome` (шаблон в `SKILL.md` §6) из артефактов всех предыдущих фаз (`run.research/*`, счётчики стадий, время по фазам из собственных заметок main) и регистрирует/закрывает slug в `_active.md`.

- ETA-базис: около 10 мин.
- Владелец, точка решения: Outcome — то место, где main отчитывается о времени по фазам, кандидатах на дореализацию заглушек (D12) и списке уроков для следующего прогона; владелец читает и утверждает здесь, не раньше.
- Итог по всей цепочке I.1–I.11 на `service-a` — около 13 ч по часам (с паузой на rate-limit внутри I.5). Считать это верхней границей, не типичным прогоном: I.5-пауза — половина этого времени.

## Приложение — библиотека и вспомогательные файлы

Не самостоятельные фазы, но каждый упомянут в спеке этой упаковки (`pipeline/SPEC.md`) и стоит за одной или несколькими командами выше.

| Путь | Роль |
|---|---|
| `pipeline/README.md` | точка входа в `pipeline/` — что где лежит, откуда читать порядок шагов (`SPEC.md`, `references/pipeline.md`) |
| `pipeline/gt.py` | диспетчер: `gt.py <stage> [флаги]`, `gt.py --list`, `gt.py --help` |
| `pipeline/run.example.json` | образец `run.json` — шесть полей, без реальных путей |
| `pipeline/check_identity_sync.py` | сверяет `IDENTITY`-литерал каждого `.js` в `pipeline/workflows/` с `gt_lib.banned_words.IDENTITY_TEXT`, ловит `Date.now(`/`Math.random(`/`new Date(` |
| `pipeline/gt_lib/__init__.py` | маркер пакета, реэкспорт `load_run` |
| `pipeline/gt_lib/paths.py` | `load_run`, `Run`, `stage_dir`, `research`, `rel`, `args_path` |
| `pipeline/gt_lib/yaml_edit.py` | точечная правка `STATUS.yaml` без полного YAML-дампа: `blocks`, `scalar`, `set_field`, `remove_block`, `get_field` |
| `pipeline/gt_lib/banned_words.py` | единый источник правды по identity-токенам и стоп-словам стиля; `IDENTITY`, `IDENTITY_TEXT`, `SENSITIVE`, `scan()` |
| `pipeline/gt_lib/journal.py` | чтение `journal.jsonl`/`<taskId>.output` результатов Workflow |
| `pipeline/gt_lib/git.py` | read-only обёртки: `ls_files`, `show`, `diff_names`, `hunks`, `removed_lines`, `head_lines` |
| `pipeline/schemas/inventory.schema.json` | форма `inventory.json` (проверяет `i1_check_research.py`, см. finder-inventory в `prompts.md`) |
| `pipeline/schemas/claims.schema.json` | форма `claims.json` |
| `pipeline/schemas/codemap.schema.json` | форма `codemap-*.json` |
| `pipeline/schemas/debt-candidates.schema.json` | форма `debt-candidates.json` |
| `templates/remap_line_refs.py` | переносится в целевой репозиторий вместе с остальными `templates/`; используется и здесь (пост-I.5), и как `tools/ground_truth/remap_line_refs.py` после установки I.9 |
| `templates/gt_mutation_selfcheck.py` | шаблон, который I.9 ставит как `tools/ground_truth/gt_mutation_selfcheck.py`; `--allow-dirty` — см. I.10 |
| `selftest/check_expected.py` | не часть прогона на реальном репозитории — проверяет результат фиктивного прогона на `selftest/fixture/`, см. `selftest/README.md` |
| `selftest/run.py` | другая половина `selftest/` — не трогает `pipeline/`: собирает одноразовый репозиторий, по очереди вносит по одной порче и проверяет, что `verify.py`/mutation-selfcheck/hooks из `templates/` ловят её с нужной серьёзностью, см. `selftest/README.md` |

Каждая стадия и каждый workflow из `pipeline/SPEC.md` §2–§6 упомянуты выше по своей фазе; этот раздел закрывает файлы, которые сами по себе фазой не являются.
