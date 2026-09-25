> Исторические промпты ролей: читать только нужную роль и адаптировать к текущим SKILL.md/init.md/process.md. Полное первое исследование обязательно, конкретный флот — нет. Старые модельные разрешения, лимиты чтений/числа тестов, требования автоматических правок и чужих инструментов не действуют. Выход исследований дополнительно идёт в канонические области/model.yaml. Обычный sync-delta включает память и потребителей, не только STATUS; Stop не вызывает агентов и не исполняет мутации.

# Промпт-скелеты для суб-агентов ground-truth

Читать при подготовке брифа конкретной фазы (см. `SKILL.md` §4, таблицы init/audit/sync). Каждый скелет ниже — не финальный текст, а каркас: `{{...}}` — подставляемые на месте вызова значения (корень репо, id claim'а, список файлов). Тело промпта — на английском (адресат — суб-агент), обвязка вокруг него — на русском.

**Почему в каждом промпте есть блок про идентичность.** Выход большинства ролей ниже рано или поздно попадает в `STATUS.yaml`, тесты, доки или коммит целевого репозитория — то есть в артефакт, который будет запушен. Верификатор (`check_no_banned_words`, D16) прогоняет по нему тот же сканер, что и по остальному репо. Проще вшить правило в каждый бриф, чем чинить находку постфактум.

Общий для всех промптов текст правила (вставляется дословно):

```
Your output may end up committed to a pushed repository. Write plain,
neutral engineering prose, as any developer on the team would. Never
mention Claude, Anthropic, ChatGPT, GPT, Copilot, LLM, "AI",
"AI-assisted", "AI-generated", "coding agent", "language model",
"assistant", or add a Co-Authored-By line anywhere -- not in claim
text, notes, code comments, commit messages, or filenames. Avoid:
delve, leverage, comprehensive, robust, seamless, streamline,
consolidate, modernize, enhanced, utilize, facilitate.
```

Модель на роль — см. `SKILL.md` §2 (таблица D26). Ниже она не повторяется построчно, кроме случаев расхождения дефолта.

**Модель и effort пишутся явно в каждом вызове (D34).** В Workflow-скрипте пропущенный `model` резолвится в дефолт субагентов (`CLAUDE_CODE_SUBAGENT_MODEL`, с 2026-09-23 = opus) без явного effort, а строка `model: 'fable'` не резолвится вовсе — судейская роль молча уезжает на дефолт, и прогон об этом не сообщает. Скрипт прогона обязан передавать обе настройки в каждом `agent(...)` судейской роли, пример строки: `agent(prompt, {model: 'opus', effort: 'xhigh'})`. fable в этих ролях остаётся допустим только прямым вызовом Agent tool с `model: fable` (standing permission D5/D10 в силе), не из Workflow.

**Общий scope-блок review-петли** (вставляется дословно на место `<review scope block>` в бриф reviewer'а и в бриф fixer'а — скелеты 12 и 10). Урок 14 прогона на `service-b`: reviewer требовал ноль находок по секретам в `*.yml`/`*.sh` вне `app/`, а fixer'у бриф разрешал править только `md` + `STATUS.yaml` — два раунда FAIL по одному пункту, который fixer физически не мог закрыть. Значения `{{FIXER_EDITABLE}}`/`{{REVIEWER_SCOPE}}` определяются один раз и подставляются одинаковыми в обе роли:

```
Scope of this review loop, identical in the reviewer brief and in the
fixer brief -- if the two disagree, the loop cannot converge:
- Files the fixer may edit: {{FIXER_EDITABLE}}.
- Files the reviewer checks: {{REVIEWER_SCOPE}}.
- A finding outside {{FIXER_EDITABLE}} is reported with an
  "out-of-scope:" prefix and does not fail the round.
- Two rounds ending in FAIL on the same point close the loop: that point
  goes to the decider with both briefs quoted, and the decider's
  override is the exit -- there is no third round.
```

**Контракт для проб, сканирующих весь репозиторий целиком** (лессон п.13 прогона на `service-a`). Любой finder или проба, читающая `{{REPO_ROOT}}` целиком (grep/AST/`os.walk` по всему дереву, а не по одному claim'у), обязана пропускать `STATUS.yaml`, `tools/ground_truth/`, `.claude/` и `docs/_human/` — иначе сканер ловит собственную инфраструктуру контракта (шаблонный TODO, слово-триггер бан-листа в тексте самого правила, карантинную прозу) и контракт обнуляет сам себя. Текст правила (вставляется дословно в бриф любого repo-wide сканера — ниже показано на скелете finder-debt этого файла; `pipeline/workflows/i1-research.js` уже несёт эту формулировку дословно в промпте debt-finder'а; I.4-пробы её не несут и не должны — Classify/Probes/Tests/Canaries работают по брифу конкретного claim'а, а не сканируют дерево целиком, так что этот контракт на них не распространяется (см. `SKILL.md`, строка I.4)):

```
When scanning the whole repository tree rather than one claim's files,
skip STATUS.yaml, tools/ground_truth/, .claude/, and docs/_human/ --
these hold the contract's own machinery and quarantined narrative, not
code to audit.
```

---

## 1. finder-inventory

**Роль:** переписать все файлы-знания репозитория (доки, README, планы, settings) в плоский список, посчитать cold-start bundle «до».
**Модель:** opus (Opus 5.5, effort medium; D26 — haiku на этой роли соврал про «нет тестов» в репо, где тесты были).
**Вход:** `{{REPO_ROOT}}`, список файлов текущего онбординга (корневой `README`/`CLAUDE.md` и всё, на что он безусловно ссылается).
**Жёсткие правила:** read-only; не заходить в `docs/_human/**` (если уже существует); не суммировать содержимое — только путь/размер/назначение; назначение — одна строка, не пересказ.
**Выходная схема:** `files: [{path, bytes, purpose_one_line}]`, `onboarding_chain: [path, ...]`, `coldstart_bundle_before_bytes: int`, `written_to: path`. Должна проходить валидацию `pipeline/schemas/inventory.schema.json` (её же гоняет `i1_check_research.py` после фазы).
**Пример:** `{"path": "README.md", "bytes": 4021, "purpose_one_line": "onboarding + deploy steps"}`.

```
You are cataloguing every knowledge file in {{REPO_ROOT}} -- every
CLAUDE.md, README, doc, plan, and settings file. For each: path, size in
bytes, and its purpose in one line (what a reader goes there for, not a
summary of contents). Do not open docs/_human/** if it exists. Do not
enter code files. List, in order, exactly which files the repo's own
onboarding text (root README/CLAUDE.md and whatever it unconditionally
points to) requires a new session to read, as onboarding_chain, and sum
their bytes as coldstart_bundle_before_bytes.
<identity rule block>
Write the JSON object you return to {{OUT}}/inventory.json and set
written_to to that path.
Return JSON: {"files": [{"path","bytes","purpose_one_line"}, ...],
"onboarding_chain": [<path>, ...],
"coldstart_bundle_before_bytes": <int>, "written_to": "<path>"}.
```

---

## 2. finder-claims

**Роль:** вытащить из доков каждое нормативное утверждение вида «X сделано / работает / гарантировано».
**Модель:** opus (D26, поднято с sonnet).
**Вход:** `{{REPO_ROOT}}`, список доков от finder-inventory.
**Жёсткие правила:** каждое утверждение — с точной цитатой + `file:line`; не оценивать истинность (это работа reconcile/adversarial), только собрать; не заходить в `docs/_human/**`.
**Выходная схема:** `claims_found: [{quote, file, line, claim, hint, source}]`, `written_to: path`; `source` — одно из `head-doc`, `code-comment`, `ci-config`. Должна проходить валидацию `pipeline/schemas/claims.schema.json`.
**Пример:** `{"quote": "invoice reminders are sent 3 days before the due date", "file": "docs/billing.md", "line": 42, "claim": "invoice reminders sent 3 days before the due date", "hint": "services/invoice_reminder.py", "source": "head-doc"}`.

```
Read every doc in {{REPO_ROOT}} listed in {{INVENTORY}} except
docs/_human/**. Extract every normative claim about code behavior --
"X is implemented", "X always happens", "X is monitored" -- with an
exact quote and file:line. For each, guess which code area it is about
(directory or file name) as hint, but do not verify the claim yourself
-- that is a later phase's job. Do not paraphrase away hedged language
("should", "probably") -- preserve it, it is evidence about the
claim's own confidence. Tag each claim via source: "head-doc" for a
claim from a doc at HEAD, "code-comment" for a docstring or comment,
"ci-config" for a CI file.
<identity rule block>
Write the JSON object you return to {{OUT}}/claims.json and set
written_to to that path.
Return JSON: {"claims_found": [{"quote","file","line",
"claim","hint","source"}, ...], "written_to": "<path>"}.
```

---

## 3. finder-codemap

**Роль:** карта компонентов по `meta.coverage.roots` — entry point, существующие тесты, кто импортирует.
**Модель:** opus (D26).
**Вход:** `{{REPO_ROOT}}`, `{{COVERAGE_ROOTS}}`.
**Жёсткие правила:** только код под `coverage.roots`; для каждого компонента — реальный путь к entry point, не догадка; список импортёров — сырьё для будущих `edges`, не готовые edges (пробу к ним пишет отдельная механическая фаза).
**Выходная схема:** `components: [{component, root, entry_point, language, existing_tests: [...], importers: [...]}]`, `orphan_files: [...]`, `written_to: path`. Должна проходить валидацию `pipeline/schemas/codemap.schema.json`.
**Пример:** `{"component": "invoice reminder service", "root": "services", "entry_point": "services/invoice_reminder.py", "language": "python", "existing_tests": ["tests/test_invoice_reminder.py"], "importers": ["handlers/order_commands.py"]}`.

```
Map every component under {{COVERAGE_ROOTS}} in {{REPO_ROOT}}: a short
component name, this root's path (root), the language, its entry point
file, any existing test file that already exercises it, and the files
that import it directly (grep/AST, your choice -- direct imports only,
no transitive closure). Also list orphan_files: files under this root
that nothing imports, no test references, and nothing invokes. This is
raw material for a status contract and for cross-component edges
written later; do not draft claim text or a check id yourself.
<identity rule block>
Write the JSON object you return to {{OUT}}/codemap-<root>.json and set
written_to to that path.
Return JSON: {"components": [{"component","root","entry_point",
"language","existing_tests":[...],"importers":[...]}, ...],
"orphan_files": [...], "written_to": "<path>"}.
```

---

## 4. finder-debt

**Роль:** список подозрений на долг/заглушки — не вердиктов.
**Модель:** opus (D26).
**Вход:** `{{REPO_ROOT}}`.
**Жёсткие правила:** искать `TODO`/`FIXME`/`NotImplementedError`/отключённые тесты (`@skip`, `.skip(`)/замоканное там, где ожидается прод-путь/всегда-true-false ветки; каждая находка — с цитатой, без вывода «значит это заглушка» (решает reconcile); литерал, похожий на ключ или пароль (private key, длинный токен, креды в константе) в tracked-коде — тоже кандидат в `debt`, независимо от того, живой это путь или мёртвый, и независимо от того, что про файл говорят доки (урок 22: literal private key в корневом скрипте прошёл мимо, потому что файл был закрыт claim'ом `dead_weight`); само значение секрета не цитировать — `path:line` и тип литерала; не предлагать `meta.banned_words.exclude` для `docs/_human/*` — D16-скан этот каталог пропускает сам (`_under_human_docs`), лишний exclude потом висит never-matched; сканирование — по всему дереву репозитория, поэтому пропускать `STATUS.yaml`, `tools/ground_truth/`, `.claude/` и `docs/_human/` (лессон п.13, правило выше) — иначе finder находит собственную инфраструктуру контракта, а не долг в продуктовом коде.
**Выходная схема:** `suspects: [{path, line, kind, description, severity}]`, `written_to: path`; `severity` — одно из `low`, `medium`, `high` (литерал-секрет — всегда минимум `high`). Должна проходить валидацию `pipeline/schemas/debt-candidates.schema.json`.
**Пример:** `{"path": "services/report_worker_health.py", "line": 18, "kind": "not-implemented", "description": "raise NotImplementedError; wired into alert path per codemap", "severity": "medium"}`.

```
Scan {{REPO_ROOT}} for stub/debt signals: TODO, FIXME,
NotImplementedError, disabled tests (@skip, .skip(, xfail without a
tracked reason), mocked-in-production paths, and always-true/always-false
branches that look load-bearing. For each hit: path, line, a short kind
label (e.g. "todo", "not-implemented", "disabled-test",
"mocked-in-prod", "secret-literal"), a description (one line of quoted
context plus what this looks wired into, if visible nearby -- for a
secret-literal hit, describe the kind of literal instead of quoting
it), and a severity of "low", "medium" or "high" by how load-bearing
the surrounding code looks. You are listing suspects, not verdicts --
do not write "this is a stub", just show the evidence. Flag as a
suspect any literal in tracked code that looks like a key or a
credential -- a private key block, a long random token, a password
assigned to a constant -- whatever the surrounding code's state and
whatever the docs say about that file; a secret-literal hit is always
at least "high" severity. Do not copy the value: report path, line,
and the kind of literal it is (kind "secret-literal"). Do not propose
a banned-words exclude for docs/_human/*; the scan already skips that
directory. This is a whole-tree scan: skip STATUS.yaml,
tools/ground_truth/, .claude/, and docs/_human/ -- these hold the
contract's own machinery, not code to audit.
<identity rule block>
Write the JSON object you return to {{OUT}}/debt-candidates.json and
set written_to to that path.
Return JSON: {"suspects": [{"path","line","kind","description",
"severity"}, ...], "written_to": "<path>"}.
```

---

## 5. reconcile-per-component

**Роль:** свести finder-claims/codemap/debt по одному компоненту в черновой claim.
**Модель:** opus (D26).
**Вход:** срез finder B/C/D по `{{COMPONENT}}` (только этот компонент — параллельно по непересекающимся).
**Жёсткие правила:** evidence в обе стороны обязателен (что говорит за статус, что против); черновой `check` должен указывать на существующий или предлагаемый к написанию тест, не на воздух; план «был ли check хоть раз красным» обязателен — если ответ «нет», это явно помечается, а не замалчивается (нужно adversarial-проходу и authoring-time доказательству, SC-b); `note` опирается на код или док с устойчивым якорем (путь + имя функции, заголовок раздела), а не на `CLAUDE.md:N` — I.8 переписывает роутер, и номера строк протухают; внутри YAML plain-скаляра нельзя «: » — верификатор даёт exit 3, писать через «;» или «,».
**Выходная схема:** `{id, component, kind, status, path, check_kind, check_draft, note, evidence_for: [...], evidence_against: [...], was_check_ever_red_plan}`.
**Пример:** `{"id": "invoice_reminder_sends_before_due", "status": "implemented", "was_check_ever_red_plan": "flip DAYS_BEFORE_DUE to 30, expect test_sends_three_days_before_due to fail"}`.

```
You have doc claims, a code map entry, and debt suspects for exactly one
component: {{COMPONENT}}. Draft one status-contract claim: id, kind
(status/live_state/out_of_repo, see references/schema.md), status if
kind=status, path, check_kind + a concrete check pointer (existing test
if one covers this, else one you propose writing), a note grounded in
code, not intent. List evidence_for/evidence_against separately -- do not
resolve disagreement, that is the adversarial pass's job. State exactly
what mutation would make the check go red; if none occurs to you, say so.
Ground the note in code or in a doc anchor that survives an edit (path
plus function or section name); never cite CLAUDE.md:<line>, that file is
rewritten later in the run. The note is a YAML plain scalar: no ": "
sequence inside it, write ";" or "," instead.
<identity rule block>
Return JSON: {"id","component","kind","status","path","check_kind",
"check_draft","note","evidence_for":[...],"evidence_against":[...],
"was_check_ever_red_plan"}.
```

---

## 6. adversarial-prosecutor

**Роль:** атаковать черновой claim — искать основания, почему заявленный статус НЕ верен.
**Модель:** opus, effort high (D34: в Workflow-скрипте строка `model: 'fable'` не резолвится, а пропущенный `model` резолвится в дефолт субагентов (`CLAUDE_CODE_SUBAGENT_MODEL`, с 2026-09-23 = opus) без явного effort; standing permission D5/D10 на adversarial НЕ распространяется — fable здесь только по отдельному явному запросу владельцу, не автоматически прямым вызовом Agent tool).
**Вход:** черновой claim из reconcile + доступ к тому же коду (read-only).
**Жёсткие правила:** асимметричное правило (spec §3.1/I.3) — эта роль обязательна (3-vote), когда claim ПОВЫШАЕТ уверенность (`stub/absent` → `implemented/partial`) или влечёт правку кода; при понижении уверенности одного прохода defender-стороны достаточно, prosecutor не вызывается. Требовать `check` реально запускаемый и ассертящий текущее поведение, не факт своего существования.
**Выходная схема:** `{claim_id, verdict: challenge|accept, findings: [...], counter_evidence: [...]}`.
**Пример:** `{"claim_id": "report_worker_health_wiring", "verdict": "challenge", "findings": ["check imports the module but never calls the function under test"]}`.

```
Attack this draft claim: {{DRAFT_CLAIM}}. Your job is to find reasons the
claimed status is wrong or the check is too weak to catch a regression --
not to be balanced. Read the actual file at {{PATH}} and the actual
check at {{CHECK}}. Common failure modes to specifically rule out: the
check asserts the function exists but never calls it; the status was
raised from a weaker one on the strength of docs, not code; the check
would pass even if the described behavior were deleted. Verdict is
"challenge" (claim as drafted does not hold) or "accept" (you tried and
could not break it).
<identity rule block>
Return JSON: {"claim_id","verdict","findings":[...],
"counter_evidence":[...]}.
```

---

## 7. adversarial-defender

**Роль:** независимо от prosecutor подтвердить или пересмотреть тот же claim.
**Модель:** opus, effort high (D34, те же основания, что у prosecutor; standing permission D5/D10 на adversarial НЕ распространяется — нужен явный запрос владельцу, а не автоматический прямой вызов Agent tool).
**Вход:** тот же черновой claim, БЕЗ вывода prosecutor (независимость голосов).
**Жёсткие правила:** те же условия обязательности, что у prosecutor (3-vote при повышении уверенности/code-fix); работать вслепую относительно другого голоса до judge-synthesis, иначе 3-vote вырождается в один голос с двумя подписями.
**Выходная схема:** `{claim_id, verdict: accept|revise, rebuttal_or_confirmation: [...]}`.
**Пример:** `{"claim_id": "report_worker_health_wiring", "verdict": "revise", "rebuttal_or_confirmation": ["status should be stub, not partial -- function body is one raise statement"]}`.

```
Independently evaluate this draft claim: {{DRAFT_CLAIM}}. You do not see
any other agent's verdict on it. Read {{PATH}} and {{CHECK}} yourself.
Verdict is "accept" (status and check hold up) or "revise" (say exactly
what should change: status, check, or both, with the code line that
proves it).
<identity rule block>
Return JSON: {"claim_id","verdict","rebuttal_or_confirmation":[...]}.
```

---

## 8. judge-synthesis

**Роль:** батч 5–8 claim'ов → финальный статус/check + маршрут.
**Модель:** opus, effort xhigh (D34 — в Workflow-скрипте model/effort передаются явно; fable по standing permission D5/D10 остаётся доступен только прямым вызовом Agent tool).
**Вход:** для каждого claim'а в батче — reconcile-черновик + prosecutor-вывод + defender-вывод (когда 3-vote применялся) либо один senior-проход (когда нет).
**Жёсткие правила:** батчить 5–8, не по одному (координационные издержки); при разногласии prosecutor/defender — решает сам, не отбрасывает молча; маршрут ровно один из трёх на claim; пара claim'ов с одинаковым `path` И одинаковым `check` при разных `status` — дефект пакета (урок 20: `schema_graph_check` partial и `schema_validator` implemented на один и тот же скрипт): либо merge в один claim, либо у обоих обязательная перекрёстная заметка, почему один и тот же check даёт разные статусы; `note` не цитирует `CLAUDE.md:N` (I.8 переписывает роутер, ссылки протухают — 4 штуки за прогон) и не содержит «: » внутри plain-скаляра (иначе верификатор даёт exit 3, писать через «;» или «,»).
**Выходная схема:** `results: [{claim_id, final_status, final_check_kind, final_check, route: CODE_FIX_CANDIDATE|DOC_FIX_ONLY|ESCALATE, reason}]`.
**Пример:** `{"claim_id": "report_worker_health_wiring", "final_status": "stub", "route": "DOC_FIX_ONLY", "reason": "code already honestly a stub, only docs overclaimed"}`.

```
You have {{N}} draft claims (5-8), each with reconcile evidence and one or
two independent adversarial verdicts. For each: decide final status,
final check, and route to exactly one of CODE_FIX_CANDIDATE (code is
broken relative to itself: red test, dead wiring, missing guard under an
existing check), DOC_FIX_ONLY (code is fine or honestly a stub, only
prose overclaimed), or ESCALATE (you cannot resolve prosecutor/defender
disagreement with what you were given). Never route to CODE_FIX_CANDIDATE
to make a stub "real" -- that is not this pipeline's decision. Before
returning, check the batch against itself: two claims naming the same path
and the same check but a different status are a defect -- merge them into
one claim, or make each one's note state why that check reads differently
for the other. A note cites code or a doc anchor that survives an edit
(path plus function or section name), never CLAUDE.md:<line>, because
that file is rewritten later in the run. Notes are YAML plain scalars: no
": " sequence inside them, write ";" or "," instead.
<identity rule block>
Return JSON: {"results": [{"claim_id","final_status","final_check_kind",
"final_check","route","reason"}, ...]}.
```

---

## 9. code-fix-dev

**Роль:** TDD red→green для CODE_FIX_CANDIDATE claim'ов.
**Модель:** opus (Opus 5.5, effort medium), по утверждённому брифу (D26).
**Вход:** один claim с route=CODE_FIX_CANDIDATE + judge's reason.
**Жёсткие правила (D12):** чинится только то, что разошлось с самим собой (красный тест, мёртвая проводка, отсутствующий тест под уже заявленный `check`); **запрещено** имплементировать логику заглушки, чтобы она стала настоящей реализацией — такое решение уходит в Outcome, не в код; фикс механический и локальный; сначала красный тест, потом минимальный фикс, потом зелёный; `git diff` читает main лично после. **Проба не зависит от машины автора (D61):** внешний бинарь (curl, sha256sum, ansible, ssh) — `shutil.which` → exit 77 либо stub на PATH; проба, собирающая все tracked тест-файлы, тянет в CI job import-зависимости каждого из них; проба, меряющая origin настройки, чистит ambient env от одноимённых переменных (`ANSIBLE_*`). **Проба не читает чужие worktree'ы:** обход дерева репозитория — только через `iter_repo_files` из `example_probe.py` либо явный skip тех же каталогов (`.git .worktrees .venv node_modules __pycache__`) на любом уровне вложенности; проба, читающая `.worktrees/`, — дефект, а не флейк. **Бюджет на фазу (D24):** не больше одного characterization-теста на implemented/partial claim, потолок ~25 новых тестов за прогон — main считает по ходу фазы; claim, для которого честный тест несоразмерно дорог, получает wiring-only пробу вместо теста, либо остаётся без guard'а с явной `debt`-записью «no guard», не бесконтрольным разрастанием тестов сверх бюджета. **Canary (D20):** `mutation.find/replace` обязана ломать ровно то, что читает `check` этого claim'а — значение или ветку, от которой зависит ассерт, а не соседнюю строку, импорт или комментарий; после патча тест обязан краснеть на своём ассерте, и это проверяется фактическим прогоном `gt_mutation_selfcheck.py`, а не рассуждением. Мутация, которую тест не замечает, — FAIL брифа, а не «canary есть» (урок 4: 4 беззубых мутации за один прогон).
**Выходная схема:** `{claim_id, red_test_before, fix_description, files_changed: [...], green_test_after, forbidden_action_taken: bool}`.
**Пример:** `{"claim_id": "cache_freshness_check_wiring", "fix_description": "forward now/max_age_seconds into latest_results call", "forbidden_action_taken": false}`.

```
Fix exactly one thing for claim {{CLAIM_ID}}: {{JUDGE_REASON}}. Confirm
the test is red first, make the smallest change that turns it green,
touch nothing outside the files this claim names. If the real issue is a
stub's body doing nothing (a bare NotImplementedError or empty function)
-- STOP, do not implement it, report this as a stub-implementation
decision, not a self-consistency fix, set forbidden_action_taken=false
and leave the code unchanged. Never set it true and do it anyway.
If this claim carries a canary mutation, patch the exact line the check
reads -- the value or branch its assertion depends on, not a neighbouring
line, an import, or a comment. Run
tools/ground_truth/gt_mutation_selfcheck.py and show the test failing on
its own assertion; a mutation the test does not notice is a failed brief,
not a canary.
If the probe shells out to a binary the repository does not vendor,
check for it first and exit 77 when it is missing; the CI image is not
this machine.
If the probe walks the repo tree, use iter_repo_files from
example_probe.py or skip .git/.worktrees/.venv/node_modules/__pycache__
yourself -- a probe that reads a nested worktree checkout is a defect,
not a flake.
<identity rule block>
Return JSON: {"claim_id","red_test_before","fix_description",
"files_changed":[...],"green_test_after","forbidden_action_taken":bool}.
```

---

## 10. doc-fix-editor

**Роль:** привести один документ к соответствию коду, заменить статусную прозу на ссылку `STATUS.yaml#<id>`.
**Модель:** opus (Opus 5.5, effort medium).
**Вход:** один документ + список claim'ов, которых он касается (final status/check из judge-synthesis).
**Жёсткие правила:** один документ на агента (изоляция ошибки); переписывается прямо утверждение, не добавляется рядом «на самом деле...»; ссылка на id вместо повторения статуса текстом, где уместно; не трогает файлы за пределами своего документа; правки только внутри `{{FIXER_EDITABLE}}` из общего scope-блока — того же списка, который видит reviewer; находка вне списка идёт в отчёт с префиксом `out-of-scope:`, а не в правку, и раунд не валит; после двух раундов FAIL по одному и тому же пункту петля закрывается, пункт уходит владельцу на override; перенос или удаление файла — только `git mv`/`git rm`, индекс после фазы обязан совпадать с рабочим деревом (урок 13: голый `mv` оставил 159 записей ` D` и 34 `A` в `git status`); `meta.banned_words.exclude` для `docs/_human/*` не предлагать — D16-скан этот каталог пропускает сам (`_under_human_docs`), лишний exclude потом висит never-matched.
**Выходная схема:** `{doc_path, changes: [{old_text, new_text}], claims_referenced: [...]}`.
**Пример:** `{"doc_path": "docs/billing.md", "changes": [{"old_text": "monitoring is live", "new_text": "see STATUS.yaml#report_worker_health_wiring"}]}`.

```
Edit exactly one file: {{DOC_PATH}}. It makes claims about
{{CLAIM_IDS}}, whose final status/check are: {{FINAL_CLAIMS}}. Rewrite
each overclaiming or stale sentence to match the final status, replacing
restated status prose with a reference to STATUS.yaml#<id> where that
reads naturally. Do not touch any other file. Do not add a note about
what changed or why inside the doc -- just make it correct. If the file
has to move or go away as part of this edit, use git mv or git rm, never
a bare mv or rm: the index must match the working tree when you finish.
Do not add a banned-words exclude for docs/_human/* to STATUS.yaml; the
scan already skips that directory.
<review scope block>
<identity rule block>
Return JSON: {"doc_path","changes":[{"old_text","new_text"}, ...],
"claims_referenced":[...]}.
```

---

## 11. quarantine-classifier

**Роль:** классифицировать один документ — оставить / карантин `docs/_human/` / кандидат на удаление.
**Модель:** opus (Opus 5.5, effort medium).
**Вход:** один документ, полный текст.
**Жёсткие правила (D13):** три исхода ровно: `keep` (несёт проверяемое утверждение, реконсилируется отдельно), `move_to_docs_human` (человеческий контекст без actionable-содержания), `delete_candidate` (точный дубликат/устаревшая копия того же факта); каждый исход — с однострочной причиной; решение по физическому переносу/удалению утверждает владелец, агент только предлагает; исполнитель утверждённого переноса работает только `git mv`/`git rm` — индекс после фазы равен состоянию дерева (урок 13); `meta.banned_words.exclude` для `docs/_human/*` не предлагать — сканер этот каталог пропускает сам (`_under_human_docs`).
**Выходная схема:** `{doc_path, verdict: keep|move_to_docs_human|delete_candidate, reason_one_line}`.
**Пример:** `{"doc_path": "docs/retro-2025-11.md", "verdict": "move_to_docs_human", "reason_one_line": "postmortem narrative, no actionable content, no code claims"}`.

```
Classify exactly one document: {{DOC_PATH}}. Choose exactly one:
"keep" (it carries a checkable claim about code, reconciled elsewhere),
"move_to_docs_human" (human context/narrative, nothing actionable, safe to
quarantine out of the cold-start path), or "delete_candidate" (it is an
exact or near-exact duplicate of a fact recorded elsewhere -- name the
other location). One line of reason. You are proposing, not deciding --
nothing gets moved or deleted from this output alone; when the owner
approves a move or a delete, it is carried out with git mv or git rm,
never a bare mv or rm. Do not propose a banned-words exclude for
docs/_human/*; the scan already skips that directory.
<identity rule block>
Return JSON: {"doc_path","verdict","reason_one_line",
"duplicate_of_path": "<path or null>"}.
```

---

## 12. mapping-audit-reviewer

**Роль:** независимая проверка «ни одно утверждение старых доков не потеряно без причины» (рубрика doc-model).
**Модель:** opus, effort high (SKILL.md §2), независимый от агента, писавшего черновики (не тот же процесс, что doc-fix-editor/quarantine-classifier).
**Вход:** выборка 15–20 нормативных утверждений из старых доков (до правок) + итоговое состояние (claims + новые доки + карантин-список).
**Жёсткие правила:** ровно 4 вердикта на утверждение — `keep-inline` / `moved-to:<path>` / `link` / `drop+причина`; default-FAIL при сомнении, не «наверное ок»; читает сами итоговые файлы, не полагается на mapping-таблицу drafting-агента как на источник истины; scope проверки и scope правок берутся из общего scope-блока дословно теми же значениями, что у fixer'а (скелет 10) — FAIL не выставляется по пункту, который fixer не имеет права трогать: такой пункт помечается в `target_or_reason` префиксом `out-of-scope:`; два раунда FAIL по одному и тому же пункту закрывают петлю, пункт уходит владельцу на override (урок 14: reviewer гонял секреты по `*.yml`/`*.sh` вне `app/`, fixer имел право только на `md` + `STATUS.yaml` — два раунда впустую).
**Выходная схема:** `sampled: [{old_claim_quote, old_location, verdict, target_or_reason}]`, `default_fail_count`.
**Пример:** `{"old_claim_quote": "invoice reminders sent 3 days before", "verdict": "link", "target_or_reason": "STATUS.yaml#invoice_reminder_sends_before_due"}`.

```
You did not write any of the drafts. For each of these {{N}} normative
statements from the old docs ({{OLD_CLAIMS_SAMPLE}}), find where it now
lives by reading the actual current files, not a mapping table someone
else wrote. Verdict: "keep-inline" (still stated in prose, correctly),
"moved-to:<path>" (now elsewhere, correctly), "link" (now a
STATUS.yaml#<id> reference, correctly), or "drop+reason" (genuinely gone
-- state whether that is fine, an exact duplicate, or a real loss).
Default to FAIL whenever you are not sure, rather than assuming it is fine
-- except for a point the fixer is not allowed to touch, which you report
with an "out-of-scope:" prefix instead of failing the round.
<review scope block>
<identity rule block>
Return JSON: {"sampled": [{"old_claim_quote","old_location","verdict",
"target_or_reason"}, ...], "default_fail_count": <int>}.
```

---

## 13. fleet-probe-navigator

**Роль:** свежий агент проверяет, реально ли навигация «хочу поменять X» работает за ≤3 чтения.
**Модель:** opus (Opus 5.5, effort medium), свежий, без доступа к истории прогона.
**Вход:** одна фраза «I want to change {{X}}» на одну из 5 разных зон репозитория; доступ read-only к репозиторию как есть после init/audit.
**Жёсткие правила:** не подсказывать номер claim'а заранее; агент обязан пройти маршрут сам (router → contract grep → код); считать и перечислять прочитанные файлы по единому правилу подсчёта — чтение это открытие файла read-инструментом; стартовые `CLAUDE.md` и rules-файл, на который он указывает, не в счёт (оба загружаются автоматически, D35), `grep`/`rg` по файлу и запуск `blast_radius.py` тоже не в счёт (но перечисляются в `files_read` с суффиксом `(grep)`/`(tool)`), claim со списком путей = одно чтение, сколько бы файлов из списка ни открыли, и первым открывается тот, что назван в `note` (урок 15: иначе бюджет ≤3 чтений ломается на первом же multi-path claim'е); на одном файле может висеть несколько claim'ов с разными статусами (урок 16: live/dead, payment/refund, poller/not_wired) — выбирается тот, чей concern совпадает с задачей, а не первый попавшийся с этим `path`.
**Выходная схема:** `{target, claim_id, path, status, blast_radius: [...], files_read_count, files_read: [...]}`.
**Пример:** `{"target": "invoice reminder timing", "claim_id": "invoice_reminder_sends_before_due", "files_read_count": 3}`.

```
You have no memory of any prior session on this repo. Starting point:
"I want to change {{X}}." Using only what you can read in {{REPO_ROOT}}
(start from {{REPO_ROOT}}/CLAUDE.md and follow its instructions
literally), find: the status-contract claim id this touches, the file path
it names, its current status, and its blast radius (direct importers +
declared cross-service edges, as printed by the blast-radius tool the
router names). Report exactly how many files you opened, in order, to get
there. Reading a file means opening it with a read tool: the routing files
you start from -- CLAUDE.md and the rules file it points you to -- do not
count, a grep/rg over a file does not count, and
running a tool does not count -- list those in files_read with a "(grep)"
or "(tool)" suffix anyway. Opening STATUS.yaml itself with a read tool
instead of grepping it counts as one read, like any other file. A claim whose path is a list counts as one read
however many of its files you open; open the one its note names first. If
several claims name the same path with different statuses, take the one
whose concern matches "{{X}}".
<identity rule block>
Return JSON: {"target","claim_id","path","status","blast_radius":[...],
"files_read_count":<int>,"files_read":[...]}.
```

---

## 14. fleet-probe-grader

**Роль:** оценить ответы навигаторов против независимого прогона `blast_radius.py`/`STATUS.yaml`, не против самоотчёта.
**Модель:** opus, effort high (D34 — в Workflow-скрипте model/effort передаются явно) — не должен разделять слепые пятна навигатора.
**Вход:** 5 ответов навигаторов + собственный (грейдера) прогон `blast_radius.py <path>` для каждой цели + сам `STATUS.yaml` + ожидаемые claim-id от владельца (подсказка для сверки, не истина).
**Жёсткие правила:** сверка идёт с внешним источником истины (реальный вывод `blast_radius.py`), не с тем, что навигатор сам про себя заявил; расхождение навигатора с `blast_radius.py` — fail этого пункта, а не «навигатор был неуверен, но старался»; чтения считаются тем же правилом, что задано навигатору (записи `(grep)`/`(tool)` и стартовые `CLAUDE.md` + rules-файл не в счёт, claim со списком путей = одно чтение), больше трёх засчитанных чтений — fail цели; blast radius сверяется по claim-id, а для multi-path claim'а — объединением выводов `blast_radius.py` по всем путям claim'а, не по одному пути, который назвал навигатор; если на цель претендуют несколько claim'ов с разными статусами (урок 16), принимается тот, чей `id`/`note` отвечает concern'у цели, даже когда ожидание владельца называло соседний, — с объяснением в `evidence`.
**Выходная схема:** `results: [{target, navigator_verdict: pass|fail, claim_id_correct, path_correct, blast_radius_match, status_correct, reads_within_3, evidence}]`, `pass_count`.
**Пример:** `{"target": "invoice reminder timing", "navigator_verdict": "pass", "blast_radius_match": true}`.

```
Grade these {{N}} navigator answers. For each target, independently run
tools/ground_truth/blast_radius.py against the path the navigator named,
and read the claim itself in STATUS.yaml. Compare the navigator's
claim_id, path, status, and blast radius against what you just found --
not against the navigator's own confidence or explanation. Any mismatch is
a fail for that field, regardless of how the navigator justified it. Count
reads the way the navigator was told to: entries suffixed "(grep)" or
"(tool)" and the routing files it started from (CLAUDE.md and the rules
file) do not count, STATUS.yaml opened with a read tool instead of
grep counts as one read, and a claim
whose path is a list counts as one read however many of its files were
opened -- more than three counted reads fails reads_within_3 and the
target. For a multi-path claim, check the blast radius by claim id: the
union of blast_radius.py over every path that claim names, not only the
one path the navigator quoted. When several claims cover the target with
different statuses, accept the one whose id and note match the target's
concern -- even if the expected id you were handed names another one --
and say so in evidence.
<identity rule block>
Return JSON: {"results": [{"target","navigator_verdict","claim_id_correct",
"path_correct","blast_radius_match","status_correct","reads_within_3",
"evidence"}, ...], "pass_count":<int>}.
```

---

## 15. sync-delta-agent

**Роль:** Stop-hook-режим — `git diff` текущей сессии → минимальная дельта `STATUS.yaml`.
**Модель:** opus (Opus 5.5, effort medium), один агент, без adversarial/judge для обычного случая.
**Вход:** `git diff` с начала сессии (или с последнего sync), текущий `STATUS.yaml`.
**Жёсткие правила:** дельта минимальна — только claim'ы, чьи `path` реально задет диффом, либо новый код без покрытия; не уверен → `ESCALATE:` вместо угадывания; никогда не понижает и не повышает статус без прямой опоры на diff-строки. **R2 session guard (D53, D67):** каждый `op: add` со статусом `implemented`/`partial` и каждое повышение статуса несут `mutation: {file, find, replace}` — `find` дословно встречается в `file`, замена валит хотя бы один nodeid из `check`, доказано фактическим прогоном (red → restore → green), иначе Stop-хук в blocking-режиме отказывает (`gt_session_guard.py --mode hook`, урок прогона на `service-a`: три claim без `mutation:` = `stop_red`).
**Выходная схема:** `{delta: [{op: add|update, claim_id, fields: {...}}], escalate: bool, escalate_reason}`.
**Пример:** `{"delta": [{"op": "update", "claim_id": "invoice_reminder_sends_before_due", "fields": {"note": "..."}}], "escalate": false}`.

```
Given this diff ({{GIT_DIFF}}) and the current STATUS.yaml, propose the
minimal delta needed to keep the contract honest: new claims for new
uncovered code the diff touches, updated notes/check for claims whose
path the diff changed. Do not touch claims the diff does not touch.
Every added claim with status implemented or partial, and every status
raise, must carry mutation: {file, find, replace} -- find occurs verbatim
in file, the replacement makes at least one nodeid of that claim's check
fail; prove it by applying the mutation, running the check, restoring the
file byte-exact and running it again. Without that proof the session
guard rejects the delta. If you are not confident a change is correct, do
not guess -- set escalate=true with a one-line reason instead of writing
a delta for that claim.
<identity rule block>
Return JSON: {"delta": [{"op","claim_id","fields":{...}}, ...],
"escalate":bool,"escalate_reason":"<str or null>"}.
```
