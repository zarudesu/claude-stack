# Проверка изменений скилла

Обязательный локальный набор без модельных вызовов:

```sh
python3 <skill>/selftest/run.py
bash <skill>/selftest/hooks_smoke.sh
python3 <skill>/selftest/process_checks.py
python3 <skill>/selftest/memory_checks.py
python3 <skill>/pipeline/check_identity_sync.py
```

run.py проверяет установленные шаблоны и детерминированные install/audit helpers;
hooks_smoke — реальные события хуков; process_checks — исполняемые гарантии
доказательств, audit checkpoints и учёта usage. Фикстуры временные. Нужны PyYAML,
pytest, git; Go/Node проверки выполняются при наличии инструментов, Java smoke
использует fake Maven. Пропуски локальных вариантов сообщать, не называть их
проверкой всех настоящих CI-окружений.

memory_checks — временная multi-repo модель: адресные packets, source/docs/
dependency drift, новые/удалённые файлы, gaps, worktrees, TTL, явный refresh,
конкуренция, hooks и отсутствие модели. Независимая проба новой сессией нужна
при существенном изменении поведения скилла: дать задачу и сырые исходники,
не предполагаемый правильный ответ. Работать только в изолированной фикстуре.
Это проверка скилла, не замена первичной приёмки каждого реального проекта
по references/init.md и не измерение экономии всех будущих моделей.

Ниже — опциональный end-to-end модельный тест старого полного конвейера. Нужен
при изменении полного workflow/промптов, которое не покрывают детерминированные
тесты, и при решении о его выпуске. Запускать с явным бюджетом; не на каждую
обычную правку шаблона или audit helper. Модели брать из доступных и выбранных
пользователем; предписание ниже (opus с effort low) — дефолт на 2026-09-23, не догма.
Изменения только обычного процесса не требуют дорогого I.1–I.10.

## 1. Materialize the fixture

```
bash <skill>/selftest/fixture/make_fixture.sh <target-dir>
```

(`<skill>` is the absolute path to this skill directory, the same value that goes into `run.json.skill` below — every command in this file uses it so none of them depend on the caller's current directory.)

`<target-dir>` must not exist or must be empty. This copies `fixture/repo/` into `<target-dir>` and commits it as a fresh one-commit git repository — the fixture's own git history plays no role, only the file contents do. See `fixture/README.md` for what the fixture repo contains (six components across Python/Go/JS, one embedded secret, four documentation claims that are false on purpose).

## 2. Write a run config

Create a `run.json` (shape: `pipeline/run.example.json`) pointing at the fixture:

```json
{
  "repo": "<target-dir>",
  "research": "<some scratch dir>/research",
  "scratch": "<some scratch dir>/scratch",
  "python": "<path to a python3 interpreter>",
  "skill": "<absolute path to this skill directory>",
  "date": "<today, YYYY-MM-DD>"
}
```

Set **every model role to `{"model": "opus", "effort": "low"}`** for this run — the fixture run proves the wiring, not the judgment quality of any one model. Opus 5.5 cannot turn thinking off, `low` is its cheapest effort level, and sonnet is no longer the default model for any role. Pass this as `--models roles.json` to every stage that builds a workflow's args file; see `pipeline/SPEC.md` §1.3 for the full role list (`inventory, finder, reconcile, defender, prosecutor, judge, audit, codefix, docfix, reviewer, navigator, grader`). `roles.json` is one flat object, role name to a model (and optional effort):

I.1 has no args-building stage — its args object is hand-written by the calling session, not produced by a `--models FILE` flag (see step 1 below). Copy the two roles the fixture's finders use (`inventory`, `finder`) out of `roles.json` into that hand-written object's `models` key yourself; otherwise the finders run on the workflow's own defaults (`finder` defaults to opus), which is exactly the phase this low-effort run is meant to cover.

```json
{
  "inventory": {"model": "opus", "effort": "low"},
  "finder": {"model": "opus", "effort": "low"},
  "reconcile": {"model": "opus", "effort": "low"},
  "defender": {"model": "opus", "effort": "low"},
  "prosecutor": {"model": "opus", "effort": "low"},
  "judge": {"model": "opus", "effort": "low"},
  "audit": {"model": "opus", "effort": "low"},
  "codefix": {"model": "opus", "effort": "low"},
  "docfix": {"model": "opus", "effort": "low"},
  "reviewer": {"model": "opus", "effort": "low"},
  "navigator": {"model": "opus", "effort": "low"},
  "grader": {"model": "opus", "effort": "low"}
}
```

## 3. Run the chain, in order

The main session issues each Workflow call itself (there is no stage that spawns a Workflow) — a stage builds the args file, main calls the Workflow with `scriptPath` + that args file, and the following stage reads the result back out of the journal. In order, for the fixture:

1. `python3 <skill>/pipeline/gt.py i1_check_research --run run.json --journal <transcript dir>/journal.jsonl` (the Workflow tool prints the transcript dir when it launches; the stage rewrites the research files from the journal before checking them) — but first produce `inventory.json`/`claims.json`/`codemap-*.json`/`debt-candidates.json` via the `i1-research.js` Workflow, called with a hand-written `args = {repo, research, roots, models}` object. `roots` is a list of objects, not names — the script throws on anything else; for the fixture (coverage roots `app`, `svc`, `web` — see `fixture/expected.json`): `"roots": [{"name": "app", "path": "app", "language": "python"}, {"name": "svc", "path": "svc", "language": "go"}, {"name": "web", "path": "web", "language": "javascript"}]`. Put the `inventory` and `finder` entries from `roles.json` under `models` — there is no stage that builds this args file for you.
2. `i2_build_slices`, `i2_build_args` → `i2-i35.js` Workflow → `i35_dump_judged --task-output <tasks dir>/<taskId>.output` (the task id is in the Workflow launch message; the journal-only fallback rebuilds items without votes), `i35_assemble_status` (produces the draft `STATUS.yaml` in the stage's `--out-dir`; the stage itself never writes into the repo). Copy that draft to `<target-dir>/STATUS.yaml` before continuing — every step from here on reads `STATUS.yaml` out of the repo, not out of scratch.
3. `i4_classify_briefs`, `i4_build_args` → `i4.js` Workflow → `i4_apply --journal <transcript dir>/journal.jsonl --dry-run`, then `--write` (this stage reads the per-agent journal, not the task output — the transcript dir is in the Workflow launch message) (folds the results into `claims-judged.json` and rebuilds the claims/debt of the repo's `STATUS.yaml` from it; anything edited by hand in the repo copy after step 2 is lost, so edit `claims-judged.json` instead).
4. `i5_build_briefs` (conflict briefs per doc / per coverage root out of `claims-judged.json`), `i5_build_args` → `i5.js` Workflow, then the post-I.5 drift chain (`remap_line_refs.py`, `i5_recon_build` → `recon-review.js` → `i5_recon_apply --write`) and, if any raise candidates came out of it, `i5_vote_args` → `raise-vote.js` → `i5_revert_raises --write`.
5. `i6_build_args` → `i6.js` Workflow.
6. `i9_install --write` to lay down `tools/ground_truth/` and the hooks.
7. `tools/ground_truth/gt_mutation_selfcheck.py --allow-dirty` (the fixture is never committed during the run, and steps 3–4 edit the same files the canaries mutate; without the flag the script refuses those files and reports a false FAIL), then `i10_build_args` → `i10-fleet.js` Workflow.

Full command lines, inputs, and outputs for each of these steps are in `references/pipeline.md` — this file only orders them for a fixture run; it does not repeat their syntax.

## 4. Check the result

```
python3 <skill>/selftest/check_expected.py --status <target-dir>/STATUS.yaml --expected <skill>/selftest/fixture/expected.json --repo <target-dir>
```

This checks, per component in `expected.json`, that a claim with a matching `path` exists at the expected `status` (and `check_kind` where given), or, where the expectation lists `or_split`, that the component's claims cover every status in that list (a concern-level split such as `implemented` + `stub` instead of one `partial` claim); that every entry in `debt_must_exist` has an open debt whose path matches and whose text carries the given keyword; that at least `canary_min` canaries exist; and that the coverage roots in `STATUS.yaml` match `coverage_roots`. It prints one line per expectation and exits 1 on any miss — a clean exit 0 is the pass condition for the whole fixture run.

A partial or failed run is expected the first time a stage's output shape changes; rerun the single failing step by hand against the fixture before assuming the whole chain is broken.
