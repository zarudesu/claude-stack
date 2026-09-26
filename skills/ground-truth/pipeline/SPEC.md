# ground-truth pipeline — file-level spec (S8 packaging)

Current project-memory addition: i9_install also installs templates/gt_context.py;
CI_EXTRA_COMMANDS includes its standalone check. It is deliberately outside
contract_lib and mutation claim tests. The model/receipts are authored and
reviewed through references/init.md and references/project-memory.md, not
fabricated by the installer. This historical STATUS pipeline alone is not
the full initial acceptance criterion. Without a model (gt_context.discover
finds none) i9_install prints `SKIP project memory`, installs neither the
helper nor its CI step, keeps an existing rule file and otherwise writes
templates/ground-truth.rule.status-only.md: the STATUS-only contract.

Staging root: `<STAGE>` — the packaging session's scratch directory for this build.
It mirrors the skill root: `pipeline/`, `templates/`, `references/`, `selftest/`, `SKILL.md`.

Read-only source roots referenced below (packaging-session paths, not part of the shipped
contract — no script in the package embeds any of them, a repository name, or a
workflow/task id; see Requirement 1 / §1.1):

- `SKILL` — the previously installed skill directory being repackaged.
- `SCRATCH` — the packaging session's scratch directory holding prior standalone scripts.
- `WF` — the packaging session's saved workflow scripts directory.
- `PRIORTOOLS` — `tools/ground_truth/` inside the repository the pipeline had most recently
  run against, used only as an implementation-diff source for G5.
- `RESEARCH` — the research directory of that same prior run.
- `PLAN`, `SPLAN` — the plan.md journals of that prior run (target repo, then skill repo),
  used only to size defaults (batch counts, ETA basis) empirically.

---

## 1. Shared decisions (binding for every group)

### 1.1 run.json

`pipeline/run.example.json` and every `--run` file carry exactly six string fields, no others:

```json
{
  "repo": "/abs/path/to/repo-or-worktree",
  "research": "/abs/path/to/pm/<slug>/research",
  "scratch": "/abs/path/to/scratch",
  "python": "/abs/path/to/python",
  "skill": "/abs/path/to/skills/ground-truth",
  "date": "YYYY-MM-DD"
}
```

`gt_lib.paths.load_run(path)` returns a frozen dataclass `Run` with those six attributes
(`repo`, `research`, `scratch`, `python`, `skill` as `pathlib.Path`; `date` as `str`).
Everything else a stage needs (repo display name, coverage roots, runner, doc list, seed,
model overrides) is a CLI flag of that stage, never a run.json field. `load_run` validates
that `repo`, `research`, `skill` exist and that `date` matches `\d{4}-\d{2}-\d{2}`; it creates
`scratch` if missing. No script contains an absolute path, a repository name, or a
workflow/task id.

### 1.2 Stage naming and dispatch

- Stage module: `pipeline/stages/<phase><verb>.py`, snake_case, phase prefix is the run phase
  with dots dropped: `i1_`, `i2_`, `i35_`, `i4_`, `i5_`, `i6_`, `i9_`, `i10_`.
- Stage name = module basename. `python3 <skill>/pipeline/gt.py <stage> --run run.json [...]`.
- The first line of the module docstring is the one-line description printed by `gt.py --list`.
- Every stage: `argparse`, `main(argv: list[str]) -> int`, `--help` exits 0, `--run` required,
  `--dry-run` wherever the stage writes into the repo or into `STATUS.yaml`.
- Workflow scripts live in `pipeline/workflows/<name>.js`, kebab-case, name = the phase
  (`i1-research`, `i2-i35`, `i4`, `i5`, `i6`, `recon-review`, `raise-vote`, `i10-fleet`).

### 1.3 Model overrides in .js

Every workflow starts with

```js
export const meta = { name: '...', description: '...', phases: [...] }
const DEFAULTS = {
  inventory:  { model: 'opus' },
  finder:     { model: 'opus' },
  reconcile:  { model: 'opus' },
  defender:   { model: 'opus', effort: 'high' },
  prosecutor: { model: 'opus', effort: 'high' },
  judge:      { model: 'opus', effort: 'xhigh' },
  audit:      { model: 'opus', effort: 'high' },
  codefix:    { model: 'opus' },
  docfix:     { model: 'opus' },
  navigator:  { model: 'opus' },
  grader:     { model: 'opus', effort: 'high' },
  reviewer:   { model: 'opus' },
}
const M = Object.assign({}, DEFAULTS, args.models || {})
```

and every `agent()` call passes `{ label, phase, schema, ...M.<role> }`. Role mapping is by
role, not by phase:

| workflow | agent | role key |
|---|---|---|
| i1-research | inventory finder | `inventory` |
| i1-research | claims / codemap / debt finders | `finder` |
| i2-i35 | reconcile drafter | `reconcile` |
| i2-i35 | defender | `defender` |
| i2-i35 | prosecutor | `prosecutor` |
| i2-i35 | judge | `judge` |
| i4 | brief classifier, probe/test/canary agents | `codefix` |
| i5 | quarantine classifier, doc editor, comment editor, fixers | `docfix` |
| i5 | doc reviewer, comment reviewer | `reviewer` |
| recon-review | batch reviewers | `reviewer` |
| recon-review | judge over actionable proposals | `prosecutor` (refutation role, opus/high) |
| raise-vote | three refuters | `prosecutor` |
| i6 | auditors A and B | `audit` |
| i10-fleet | navigators | `navigator` |
| i10-fleet | grader | `grader` |

`args.models` values are objects `{model, effort?}`; a caller overriding one role keeps the
defaults of the others. Python stages that build args files accept `--models <file.json>` and
copy the parsed object into the args file under `models` (absent flag → key omitted).
No `Date.now()`, `Math.random()`, `new Date()` in any `.js`.

### 1.4 Result shape of every workflow

Each workflow returns one object; the tool wraps it as `{"result": {...}}` in
`<tasks>/<taskId>.output`, and each agent result also lands in `journal.jsonl` as
`{"type":"result","result":{...}}` (the fallback path every python stage keeps, per lesson
п.18). The returned object carries the payload keys of its source script unchanged
(so the reading stages stay compatible) plus three common keys:

- `ok` — boolean, true when every phase produced at least the results it was given inputs for;
- `counts` — `{ "<phase>": { "ok": n, "failed": n } }`;
- `failed` — array of `{ label, phase, reason }` for every agent that returned nothing.

Payload keys per workflow (unchanged from the source scripts):
`i1-research` → `{ inventory, claims, codemap, debt }` (each = the finder's JSON path plus counts);
`i2-i35` → `{ items, judged, batch_defects, failed_components, failed_batches, routes }`;
`i4` → `{ classes, probes, tests, canaries }`;
`i5` → `{ docs, comments, overrides, proposals }`;
`recon-review` → `{ results, missing }` with per-batch `{ batch, proposals, verdicts }`;
`raise-vote` → `{ votes }` with per-claim `{ id, verdicts: [{refuted, reason, evidence}] }`;
`i6` → `{ audits }` (A and B);
`i10-fleet` → `{ probes, grade }`.

### 1.5 Where stages write

- Durable artifacts the next phase and the Outcome need → `run.research/`:
  `inventory.json`, `claims.json`, `codemap-*.json`, `debt-candidates.json`, `slices/`,
  `slices-index.json`, `claims-judged.json`, `i4-triage.json`, `i4-groups.json`,
  `i4-brief-classes.json`, `i4/{probe,test,canary}/*.json`, `i4-results.json`,
  `i5/`, `i6/`, `raise-candidates.json`.
- Everything else (args files, batches, reports, STATUS backups, temp trees) →
  `run.scratch/<phase>/`, where `<phase>` is the stage's phase id:
  `i1/`, `i2/`, `i35/`, `i4/`, `i5/`, `recon/`, `vote/`, `i6/`, `i9/`, `i10/`.
  Args files are `run.scratch/<phase>/<phase>-args.json` (recon batches:
  `run.scratch/recon/batch-NN.json`; pass 2: `run.scratch/recon/pass2/batch-NN.json`).
- The STATUS.yaml draft and its side files from `i35_assemble_status` go to the stage's
  `--out-dir` (runbook default `run.scratch/i35/`), the owner copies the approved draft into
  the repo.
- Nothing is written inside the repository except by `i4_apply`, `i5_recon_apply`,
  `i5_revert_raises` and `i9_install`, all of which require `--write`.

### 1.6 Scratch one-offs explicitly not ported

One-off scratch scripts from earlier runs (repo-specific generators, hand edits, exploration
probes) are not ported; the reusable parts were folded into generic stages: line-reference remapping
into `templates/remap_line_refs.py`, recon batching into `i5_recon_build` (second pass via `--pass 2`),
basename expansion into `i5_recon_apply`.

---

## 2. G1 — core: gt_lib + dispatcher

| target | sources | contract |
|---|---|---|
| `pipeline/gt_lib/__init__.py` | new | Package marker; re-exports `load_run`. No side effects, no I/O at import. |
| `pipeline/gt_lib/paths.py` | run.json shape above; hardcoded consts in `SCRATCH/build_slices.py` (`R`, `REPO_PREFIX`), `SCRATCH/build_i4_args.py` (`R`, `S`), `SCRATCH/dump_judged.py` (`R`) | `load_run(path) -> Run` (six fields, validation per §1.1). Helpers: `stage_dir(run, phase)` (creates `run.scratch/<phase>`), `research(run, *parts)`, `rel(run, p)` (absolute → repo-relative, replaces every `REPO_PREFIX` constant), `args_path(run, phase)`. Pure stdlib. |
| `pipeline/gt_lib/yaml_edit.py` | `SCRATCH/gt_yaml_edit.py` | Same API, unchanged behaviour: `blocks(text) -> {id: block}`, `scalar(s)`, `set_field(t, id, field, new)`, `remove_block(t, id)`, `get_field(t, id, field)`. `blocks()` returning a dict keyed by id is documented (п.18). `yaml` import stays lazy inside `get_field`. |
| `pipeline/gt_lib/banned_words.py` | `SCRATCH/assemble_status.py` (`BANNED_WORDS`, `_BANNED_RE`, `_CLAUDE_MD_RE`), `SCRATCH/mkclaims.py` (`SENSITIVE`, `ALLOW`), IDENTITY strings in `SCRATCH/gt-prior-i2-i35.js`, `gt-prior-i4.js`, `gt-prior-i5.js`, `gt-prior-i6.js`, `gt-prior-i10-fleet.js`, `gt-prior-raise-vote.js`, `WF/<i1-research workflow script>`, `WF/<line-reconcile workflow script>` | Single source of truth. `IDENTITY: list[str]` = union of the identity tokens found in those files, then the eleven style words, in this fixed order; `IDENTITY_TEXT: str` = the exact paragraph every `.js` embeds (built from `IDENTITY`, wrapped, stable); `SENSITIVE: list[tuple[re.Pattern, str]]` (ipv4, domain, hostname, ssh-user) and `SENSITIVE_ALLOW`; `scan(text) -> list[hit]`; `CLAUDE_MD_RE`. Contains no repo-specific allow entries. |
| `pipeline/gt_lib/journal.py` | `SCRATCH/dump_judged.py` lines 1–35 | `read_journal(path) -> list[dict]` (one entry per `type=="result"` line, `{label, phase, result}`); `by_label(entries)`, `results_where(entries, key)`; `task_output(path)` reads a `<taskId>.output` file tolerating a text prefix before the JSON. Callers pass `--journal` or `--session-dir` + `--workflow-id`; the module builds `<session-dir>/subagents/workflows/<workflow-id>/journal.jsonl` and never embeds an id. |
| `pipeline/gt_lib/git.py` | `PRIORTOOLS/remap_line_refs.py` (`parse_diff`, `build_mapper`), `SCRATCH/comment_only_guard.py` (`head`), `SCRATCH/recon_build.py` (`head_lines`, `wt_lines`), `SCRATCH/build_recon2.py` (`get_hunks`) | Thin `subprocess` wrappers, all take `repo: Path`: `ls_files(repo, *globs)`, `show(repo, ref, path) -> str|None`, `diff_names(repo, base="HEAD")`, `hunks(repo, base="HEAD", path=None) -> {path: [(old_start, old_len, new_start, new_len)]}`, `head_lines(repo, path)` with a per-process cache. Read-only: never runs a state-changing git command; raises on non-zero exit except `show` of a missing path. |
| `pipeline/gt.py` | new | `gt.py <stage> [args...]` imports `pipeline/stages/<stage>.py` and returns `main(argv)`; `gt.py --list` prints stage name + first docstring line, sorted by phase then name; `gt.py --help` prints usage and the list. Unknown stage → exit 2 with the list. Adds the pipeline root to `sys.path`; no other side effects. |
| `pipeline/check_identity_sync.py` | `pipeline/gt_lib/banned_words.py`, `pipeline/workflows/*.js` | Extracts the `const IDENTITY = \`...\`` literal from every `.js` in `pipeline/workflows/`, normalises whitespace, compares with `banned_words.IDENTITY_TEXT`; also fails when a `.js` contains `Date.now(`, `Math.random(` or `new Date(`. Prints one line per file, exit 1 on any mismatch. No `--run` (skill-internal check). |
| `pipeline/run.example.json` | §1.1 | The six fields with neutral placeholder values, no real paths. |
| `pipeline/stages/__init__.py` | new | Empty. |
| `pipeline/README.md` | new | Placeholder: one paragraph, pointer to `references/pipeline.md`; G6 replaces it. |

---

## 3. G2 — I.1 research + I.2 slices

| target | sources | contract |
|---|---|---|
| `pipeline/workflows/i1-research.js` | `WF/<i1-research workflow script>` | `args {repo, research, roots, models?}`; four finders — inventory (`M.inventory`), claims / codemap / debt (`M.finder`) — each with its schema, each writing its JSON into `args.research`. `roots` replaces the repo-specific codemap split: one codemap finder per entry in `args.roots` (`[{name, path, language}]`). Keeps the SENSITIVE paragraph and the debt-finder secret rule (key-looking literal in tracked code → debt candidate with `path:line`, independent of what the docs say). Result per §1.4. |
| `pipeline/schemas/inventory.schema.json` | `INVENTORY_SCHEMA` in the same `.js`; `RESEARCH/inventory.json` | JSON Schema draft-07 fragment: required keys and types only, no repo-specific enums. |
| `pipeline/schemas/claims.schema.json` | `CLAIMS_SCHEMA`; `RESEARCH/claims.json` (`claims_found[]`) | Same rule; documents `claims_found[].{quote,file,line,claim,hint,source}`. |
| `pipeline/schemas/codemap.schema.json` | `CODEMAP_SCHEMA`; `RESEARCH/codemap-python.json`, `codemap-ansible.json` (`components[]`) | `components[].{component,root,entry_point,language,paths,...}`. |
| `pipeline/schemas/debt-candidates.schema.json` | `DEBT_SCHEMA`; `RESEARCH/debt-candidates.json` (`suspects[]`) | `suspects[].{path,line,kind,description,severity}`. |
| `pipeline/stages/i1_check_research.py` | the four schema files; `RESEARCH/*.json` as shape reference | `i1_check_research --run run.json --journal <transcript>/journal.jsonl [--strict]` (or `--session-dir DIR --workflow-id ID`). With a journal it first rewrites the four JSONs in `run.research` from the finders' structured return values (the validated payload; the file an agent writes by hand is not validated and on the fixture run one finder skipped its file, another used a different top-level key), then reads the four JSONs from `run.research`, checks required keys and value types against `pipeline/schemas/*.json` with a small stdlib validator (no jsonschema dependency), prints a count table (claims, components per codemap, debt suspects, inventory entries) and every violation. Exit 1 on a missing file or a type error; `--strict` also fails on unknown top-level keys. |
| `pipeline/stages/i2_build_slices.py` | `SCRATCH/build_slices.py` | `i2_build_slices --run run.json [--dry-run]`. Reads `claims.json{claims_found}`, `debt-candidates.json{suspects}` and every `codemap-*.json{components}` from `run.research`; writes `run.research/slices/<component>.json` and `run.research/slices-index.json` (`[{component, slice_path, root, entry_point, language, n_claims, n_debt}]`). Repo prefix comes from `run.repo` via `paths.rel`. Fix (п.14): an unmapped bucket takes the area path itself as `component` (`pkg/core`), never a sentence such as `unmapped items under X`. |
| `pipeline/stages/i2_build_args.py` | `SCRATCH` args convention (`slices-args.json`), `RESEARCH/slices-index.json` | `i2_build_args --run run.json [--models FILE]`. Writes `run.scratch/i2/i2-args.json` = `{repo, research, slices: [...slices-index.json...], models?}`. Prints the slice and claim counts so the operator can size the ETA. |
| `pipeline/workflows/i2-i35.js` | `SCRATCH/gt-prior-i2-i35.js` (not `.v1`) | Phases Reconcile / Adversarial / Judge with `DRAFT_SCHEMA`, `DEFENDER_BATCH_SCHEMA`, `PROSECUTOR_BATCH_SCHEMA`, `JUDGE_SCHEMA` unchanged; roles per §1.3; one drafter per component, defender over all drafts of a component, prosecutor only on confidence-raising claims, judge in batches of 5–8. Header comment documents `args` and the result shape; verdict key names (`final_status`, `final_check`, `final_path`, `final_note`, `merged_into`, `code_fix_brief`, `batch_defects_fixed`) stay as in the source. |

---

## 4. G3 — I.3.5 dump + assemble, I.4 code-fix

| target | sources | contract |
|---|---|---|
| `pipeline/stages/i35_dump_judged.py` | `SCRATCH/dump_judged.py` | `i35_dump_judged --run run.json (--journal PATH | --session-dir DIR --workflow-id ID) [--task-output PATH]` — `--task-output` предпочтителен: journal-фолбэк восстанавливает items из reconcile-результатов без голосов defender/prosecutor. Prefers the task output, falls back to the journal (judge batches = results carrying `results` + `batch_defects_fixed`); writes `run.research/claims-judged.json` (`{items, judged, batch_defects, failed_components, failed_batches, routes}`) and prints the dedup/route report (routes, kinds, statuses, check inventory against the worktree). No ids in the file. |
| `pipeline/stages/i35_assemble_status.py` | `SCRATCH/assemble_status.py` | `i35_assemble_status --run run.json --repo-name NAME --roots a,b,c [--runner pytest|go|js|none] [--out-dir DIR] [--audited YYYY-MM-DD]`. Templates dir = `run.skill/templates`; banned words and the `CLAUDE.md:N` rule come from `gt_lib.banned_words`; `--audited` defaults to `run.date`; out-dir defaults to `run.scratch/i35`. Outputs unchanged: `STATUS.yaml` draft, `escalate.json`, `code-fix-candidates.json`, `canary-candidates.json`, `assemble-report.json`. Carries the component-naming fix of `i2_build_slices` through untouched (never rewrites a component name into prose). A `final_path` entry that does not exist is never emitted: it is replaced by the first existing file the judge cited (debt `path`/description first, then `final_note`), else the nearest existing ancestor directory, and the `path_missing` defect records `substituted` (null = no anchor found, fix the judged item). |
| `pipeline/stages/i4_classify_briefs.py` | `PLAN` entry R9-prep; `RESEARCH/i4-triage.json`, `i4-groups.json`, `i4-brief-classes.json`; `SCRATCH/dump_judged.py` triage part | `i4_classify_briefs --run run.json`. Deterministic part: from `claims-judged.json` plus a repo scan it writes `run.research/i4-triage.json` (`{required[], optional[], probes[]}` — pytest node ids missing from the worktree, probe files to be written) and `run.research/i4-groups.json` (`{tests_by_file}`), and the brief list `run.scratch/i4/classify-briefs.json`. Module docstring records the decision: on the last run the class labels (`test_only`/`probe_only`/`mixed`/`prod_change`, `self_inconsistency`, `prod_files`) came from a sonnet classifier over the CODE_FIX briefs, so they stay agent-produced — the classifier is the first phase of `i4.js` (role `codefix`), and `i4_apply` folds its results into `run.research/i4-brief-classes.json` for the owner list in the Outcome. Class labels are never an input to brief building: D12 forbids production edits in I.4 for every brief regardless of class. |
| `pipeline/stages/i4_build_args.py` | `SCRATCH/build_i4_args.py` | `i4_build_args --run run.json [--canaries N] [--models FILE]`. Reads `claims-judged.json`, `i4-triage.json`, `i4-groups.json`, `canary-candidates.json` (canary claim ids come from that file, never a literal list; `--canaries` caps the count). Writes per-brief files under `run.research/i4/{probe,test,canary}/*.json` and `run.scratch/i4/i4-args.json` = `{repo, python, tmp_base, brief_dir, classify, probe_groups, test_groups, canaries, models?}`; `tmp_base` = `run.scratch/i4/tmp`, `python` = `run.python`, probe chunk size 3, one test group per test file. Prints group counts and the one-test-per-claim check (a test file whose claims share a node id is flagged, D24). |
| `pipeline/workflows/i4.js` | `SCRATCH/gt-prior-i4.js` | Phases Classify / Probes / Tests / Canaries, all `M.codefix`; schemas `CLASS_SCHEMA` (new, small: `{entries: [{claim_id, class, prod_files, self_inconsistency, summary}]}`), `PROBE_SCHEMA`, `TEST_SCHEMA`, `CANARY_SCHEMA` unchanged. Reads `args` as written by `i4_build_args` (no compact `pg`/`tg`/`cn` remapping). Keeps the hard rules of the source COMMON block: run pytest through `args.python` with `PYTHONDONTWRITEBYTECODE=1 -p no:cacheprovider`, red demo only in a copy under `tmp_base`, no state-changing git, no edits outside the brief scope, no production edits. Result per §1.4. |
| `pipeline/stages/i4_apply.py` | `SCRATCH/apply_i4.py` | `i4_apply --run run.json (--journal PATH | --session-dir DIR --workflow-id ID) [--nodes FILE] [--head-override FILE] [--write] [--dry-run]`. Folds probe/test/canary results into `run.research/claims-judged.json` (only with `--write`) and writes `run.research/i4-results.json` and `run.research/i4-brief-classes.json` (from the Classify phase). Repo prefix from `run.repo`; `--head-override` keeps the tab-separated `exit\tbasename\truntime` format. Prints the counters (`probe_not_green_at_head`, `test_not_green`, canary verdicts) unchanged. A reported test `node_id` is resolved against the file as written (AST): exact hit as is, one same-named class-wrapped node -> that id (`node_resolved`), otherwise dropped (`node_not_in_worktree`); both flags show in `--dry-run`. |

---

## 5. G4 — I.5 docs, line drift, reconcile, raise vote

| target | sources | contract |
|---|---|---|
| `pipeline/stages/i5_build_args.py` | `SCRATCH/i5-args.json` shape; `PLAN` entry R9-prep-I5 | `i5_build_args --run run.json [--status PATH] [--models FILE]`. Docs = `git ls-files '*.md'` in `run.repo` minus `docs/_human/**`; areas = coverage roots read from `meta` of `STATUS.yaml` (`--status` defaults to `run.repo/STATUS.yaml`); per-doc and per-area conflict files are read from `run.research/i5/`. Writes `run.scratch/i5/i5-args.json` = `{repo, python, guard, docs, areas, models?}`, `guard` = the `gt.py i5_comment_guard` command line. Prints the doc count and warns above 60 (D39: review phase goes into a second workflow run). |
| `pipeline/workflows/i5.js` | `SCRATCH/gt-prior-i5.js` | Two parallel pipelines: Docs (quarantine classifier → editor → reviewer → fixer, max 2 FAIL rounds, then main override) and Comments (comment editor → guard → reviewer → fixer). Roles: editors/classifiers/fixers `M.docfix`, reviewers `M.reviewer`. Schemas `CLASS/EDIT/REVIEW/CEDIT/CREVIEW` unchanged; the scope block is passed verbatim to editor, reviewer and fixer. The editor brief carries the list of claims and debts citing the document and the agent returns `citing_entries_touched: [{id, old_anchor, new_anchor|stale}]` (п.16). Header comment: above 60 documents, run the review phase as a second workflow (D39, cap 8). |
| `pipeline/stages/i5_comment_guard.py` | `SCRATCH/comment_only_guard.py` | `i5_comment_guard --run run.json [--file PATH ...]`. Default file set = modified tracked files of `run.repo`. Compares the HEAD text and the working-tree text with comments and docstrings removed (python AST, YAML with opaque tags, `.j2`, generic line-comment fallback); prints `OK` / `CHANGED-STRUCTURE` / `UNSUPPORTED` per file, exit 1 on any non-OK. Read-only. |
| `templates/remap_line_refs.py` | `PRIORTOOLS/remap_line_refs.py` merged with `SCRATCH/prose_remap.py` | One tool, `remap_line_refs.py [--base HEAD] [--apply] [--dry-run] [--report PATH] STATUS_FILE`. Rewrites `path:N`, `path:N-M`, comma/`and` lists, detached `:N` and prose forms `line N` / `lines N-M and P-Q` from base numbering to the working tree. Library API `parse_diff`, `build_mapper`, `process_value`, `process_status_file` stays importable. The report is JSON (`--report x.json`) with `rewritten`, `deleted`, `unparsed`, and `assoc_risk`: an entry naming two or more modified files in one sentence with a detached or prose citation is reported, never rewritten silently (п.17b, run journal R11). Dry run by default; `--apply` rewrites atomically and re-validates with `yaml.safe_load`. Ships into repositories: no identity tokens, plain prose. |
| `pipeline/stages/i5_recon_build.py` | `SCRATCH/recon_build.py` + `SCRATCH/recon_batches.py` + `SCRATCH/build_recon2.py` | `i5_recon_build --run run.json [--status PATH] [--pre PATH] [--report PATH] [--pass 1|2] [--unbatched FILE] [--models FILE]`. Selection rule (п.18): every STATUS record whose anchors — `path:N`, ranges, detached `:N`, prose `line N` — overlap a hunk of `git diff HEAD`, not only the records the remap rewrote. Flags `low_sim`, `deleted`, `substantive`, `assoc_risk`, `prose_far`, `replaced`, `unparsed`, `quoted_removed` (п.37: note/description repeats 5+ consecutive words of a line `git diff HEAD` removed; no anchor needed, cite carries the deleted text). Writes `run.scratch/recon/batch-NN.json` (heavy 9 entries, light 14; pass 2 into `run.scratch/recon/pass2/`), `run.scratch/recon/recon-items.json` and `run.scratch/recon/recon-args.json` = `{repo, python, date, batches, models?}`. |
| `pipeline/workflows/recon-review.js` | `WF/<line-reconcile workflow script>` | Phases Review (one batch per agent, `M.reviewer`) and Judge (`M.prosecutor`, refutes every actionable proposal). `PROPOSAL_SCHEMA` / `VERDICT_SCHEMA` unchanged. Header documents the proposal shape `{id, field, action, new_text, evidence, confidence, raise_candidate, escalate}`, the verdict shape `{id, upheld, reason, corrected_text}` and the result shape `{results:[{batch, proposals, verdicts}], missing, ok, counts, failed}`. Read-only for agents (no state-changing git, no edits). |
| `pipeline/stages/i5_recon_apply.py` | `SCRATCH/apply_recon.py` + `SCRATCH/expand_basenames.py` / `expand_basenames2.py` / `expand2a.py` / `expand2b.py` | `i5_recon_apply --run run.json --results FILE [--skip-ids FILE] [--status PATH] [--write] [--dry-run]`. Step 1: basename disambiguation — a bare filename in `new_text`/`corrected_text` that resolves to exactly one tracked path is expanded, ambiguous ones are reported and left alone. Step 2: apply per verdict — `rewrite` (anchor validated against the working tree, `bad_anchor` refused), `resolve`, `remove`; a `resolve` on a debt whose `ref` claim is not implemented is rejected and logged (the gap is in the code; the reviewer rewrites the description instead). `raise_candidate` proposals are written to `run.research/raise-candidates.json` and never applied. Backs up `STATUS.yaml` to `run.scratch/i5/STATUS.pre-recon<pass>.yaml` before writing; prints the `skipped_manual/rewrite/resolve/remove/rejected/unjudged/escalate/raise` log. |
| `pipeline/stages/i5_vote_args.py` | `SCRATCH/vote-args.json` shape; `RESEARCH` raise candidates | `i5_vote_args --run run.json [--models FILE]`. Reads `run.research/raise-candidates.json`, resolves each claim's `check`, `docs` and `code` paths from `STATUS.yaml`, writes `run.scratch/vote/vote-args.json` = `{repo, python, date, claims:[{id, check, docs, code}], models?}`. |
| `pipeline/workflows/raise-vote.js` | `SCRATCH/gt-prior-raise-vote.js` | Phase Refute: three independent refuters per claim, `M.prosecutor`, `VERDICT` schema `{refuted, reason, evidence}`. Read-only brief, grep `STATUS.yaml` by id rather than reading it whole. Result `{votes:[{id, verdicts}], ok, counts, failed}`. |
| `pipeline/stages/i5_revert_raises.py` | `SCRATCH/revert_raises.py` (pattern only) | `i5_revert_raises --run run.json --votes FILE --backup FILE [--status PATH] [--write] [--dry-run]`. Generic: for every claim whose majority verdict is `refuted`, restores `status`, `check` and `note` from the pre-vote `STATUS.yaml` backup through `gt_lib.yaml_edit`, and re-opens any debt the raise had resolved. No repo-specific edit list, no document edits. Prints one line per reverted id. |

---

## 6. G5 — I.6 audit, I.9 installer, I.10 acceptance, selftest check

| target | sources | contract |
|---|---|---|
| `pipeline/stages/i6_build_args.py` | `SCRATCH/i6-args.json` shape; `RESEARCH/i6/`; `SCRATCH/gt-prior-i6.js` | `i6_build_args --run run.json [--n 18] [--seed N] [--models FILE]`. Samples N checkable statements from the pre-edit text of the docs (`git show HEAD:<doc>`), deterministic under `--seed`, one statement per line with `doc` and `line`; writes `run.research/i6/sample.json` and `run.scratch/i6/i6-args.json` = `{repo, python, sample_path, out_dir, docs, models?}` (`out_dir` = `run.research/i6`). |
| `pipeline/workflows/i6.js` | `SCRATCH/gt-prior-i6.js` | Phase Audit: auditor A over the fixed sample, auditor B self-sampling 15–20 statements from the pre-edit docs, both `M.audit`, `AUDIT_SCHEMA` unchanged, verdicts `keep-inline` / `moved-to` / `link` / `drop+reason`, default FAIL on doubt. Read-only; cites `file:line` instead of copying values. |
| `pipeline/stages/i9_install.py` | `SKILL/SKILL.md` §5 (facts + install table + merge rules), `SKILL/templates/*` (`settings-hooks.json`, `ci-github.yml`, `ci-github-job-addition.yml`, `ci-gitlab-addition.yml`, `pre-commit-addition.yaml`, `ground-truth.rule.md`, hook scripts, `status_contract_*.template`), `PLAN` entry R9-prep-I9 | `i9_install --run run.json [--facts] [--dry-run] [--write] [--pkg PATH] [--js-test-dir PATH] [--java-pkg DOTTED] [--ci-file PATH]`. Gathers the §5 facts: `git ls-files .claude` (committed vs local hooks), `.github/workflows/*.yml` / `.gitlab-ci.yml`, `.pre-commit-config.yaml`, python runner (`pytest.ini`, `pyproject.toml [tool.pytest.ini_options]`, `setup.cfg [tool:pytest]`, `tox.ini [pytest]`, else a `tests/` directory holding `*.py`), `go.mod`, `package.json` with jest or vitest (root first, then up to two levels deep outside `node_modules`; one without a runner prints `WARN js present, runner not detected -> bridge skipped, use a probe`), `pom.xml` + `src/test/java` (the `maven` fact: present, test root, shallowest test package -- `--java-pkg DOTTED` overrides it), `.venv/bin/python` presence. `--facts` prints them as JSON and exits 0. Installs from `run.skill/templates`: `tools/ground_truth/{contract_lib,verify,blast_radius,gt_mutation_selfcheck,coldstart_budget,remap_line_refs}.py`, `tools/ground_truth/probes/` with the example probe (`chmod +x`), the bridge test renamed per runner, the two hook scripts into `.claude/hooks/` or `tools/ground_truth/hooks/` (`chmod +x`), the `Stop`/`SessionStart` entries merged into `.claude/settings.json` or `.claude/settings.local.json` (arrays merged by command string, never replaced), `.claude/rules/ground-truth.md` with `{{PY}}` resolved, the CI job into the existing CI file or a new `.github/workflows/ground-truth.yml` (also when there are several workflows and no `--ci-file`; a repo with no CI whose `origin` URL contains `gitlab` gets a new `.gitlab-ci.yml`; with no python runner the job installs `pyyaml pytest` instead of `requirements-dev.txt`; a job already in the file is not left alone: the verifier commands it does not run yet -- `gt_mutation_selfcheck.py --strict`, `gt_session_guard.py --mode ci` -- are appended next to its `verify.py --mode=full` line, and a one-line `run:` scalar becomes a block to fit them), and the pre-commit entry only when `.pre-commit-config.yaml` already exists. `drift_report(repo, facts, templates_dir)` classifies every one of those artifacts as `same`/`outdated`/`missing` without writing anything (byte comparison via `sync_file(write=False)` for the files it fully owns; for the CI job and the settings hooks block, which it merges into a file it does not fully own, presence of the commands -- the CI verifier commands, and the hook commands with the `hooks_dir` rewrite an untracked `.claude` gets, so drift asks for exactly what install writes); `audit_scope` imports this function so install and audit never disagree on a file's state. Idempotent: a second run produces no diff. `--dry-run` (default) prints the plan; `--write` applies. Never runs git commands that change state. |
| `pipeline/stages/i10_build_args.py` | `SCRATCH/i10-args.json` shape; `SCRATCH/gt-prior-i10-fleet.js` | `i10_build_args --run run.json [--seed N] [--n 5] [--status PATH] [--models FILE]`. Picks fleet-probe targets from `STATUS.yaml` deterministically under `--seed`: spread across coverage roots, preferring files carrying two or more claims. Each target = `{x, expect}` where `x` is a "where would you change …" question built from the claim, and `expect` names the target claim id plus the sibling claim ids on the same path with a one-line why-not each. Writes `run.scratch/i10/i10-args.json` = `{repo, python, targets, models?}`. |
| `pipeline/workflows/i10-fleet.js` | `SCRATCH/gt-prior-i10-fleet.js` | Phase Acceptance: fresh navigators (`M.navigator`, `NAV_SCHEMA`) answering with a claim id in three reads or fewer, then one grader (`M.grader`, `GRADE_SCHEMA`) checking each answer against `blast_radius.py` and `STATUS.yaml`, not against the navigator's self-report. Result `{probes, grade, ok, counts, failed}`. |
| `pipeline/stages/audit_scope.py` | `references/process.md`, `references/pipeline.md` audit | `audit_scope --run run.json [--status PATH] [--max-age-days 30] [--sample 5] [--seed N] [--skip-verify]`. Compares against last substantive audit commit, including dirty/untracked paths, check targets, changed/removed claims, coverage, edges/debt. Fresh full checks plus no relevant drift and recent age allow SHORT-CIRCUIT, with no model workflow or audit date refresh. Outputs scratch/audit/audit-scope.json with evidence, sample and template drift. Missing base or skipped verification never short-circuits. No repo writes. |
| `pipeline/stages/audit_close.py` | `references/process.md` | `audit_close --run run.json --reviewed [--status PATH] [--write] [--force]`. Requires caller-attested substantive review, scope HEAD match and a fresh full check. --force permits initial close without scope, never bypasses checks. Writes date/commit only with --write and preserves unrelated lines/comments. |
| `templates/gt_mutation_selfcheck.py` | `PRIORTOOLS/gt_mutation_selfcheck.py`, diffed against `SKILL/templates/gt_mutation_selfcheck.py` | Replace the template with the repo version: `--allow-dirty` flag, `_run_one(root, claim, allow_dirty=False)`, dirty check skipped under the flag; restore always from the saved bytes in `finally`. Keep every template-only improvement found in the diff (the current diff is exactly the four `--allow-dirty` hunks, so nothing else is at risk). Default without the flag stays strict for the Stop hook and CI. |
| `selftest/check_expected.py` | `SKILL/selftest/fixture/expected.json`, `SKILL/selftest/fixture/make_fixture.sh`, `SKILL/selftest/run.py` (style) | `check_expected.py --status PATH --expected PATH [--repo PATH]`. For each expected component: a claim whose `path` contains that component exists with the expected `status` (and `check_kind` when given). For each `debt_must_exist`: an open debt whose `path` matches and whose text carries the keyword. At least `canary_min` canaries. Coverage roots of `STATUS.yaml` equal `coverage_roots`. Prints a table (`expectation | found | verdict`), exit 1 on any miss. Stdlib plus `yaml`. |

---

## 7. G6 — runbook, SKILL.md, prompts, selftest doc

| target | sources | contract |
|---|---|---|
| `references/pipeline.md` | this spec; `SPLAN` (ETA basis, lessons п.12–п.20, D40 Monitor rule); `PLAN` (phase timings); `SKILL/SKILL.md` §4.1 | The runbook. One section per chain step I.1 → I.11 including the post-I.5 drift chain, raise-vote, the I.9 installer and I.10: exact command (`python3 <skill>/pipeline/gt.py <stage> --run run.json ...`) or Workflow call (`scriptPath = <skill>/pipeline/workflows/<x>.js`, args file path), inputs, outputs, ETA basis from the last run (I.1 43 min; I.2–I.3.5 about 2 h 45; I.4 69 min; I.5 about 4 h wall including a rate-limit pause; recon pass 40 + 17 min; I.6 about 20 min; fleet-probe about 5 min per round, not 25), owner touchpoints (I.5 delete/quarantine list approved before action; I.11 Outcome), and the Monitor backstop for any run longer than 10 minutes. Every file listed in this spec is referenced by path. |
| `SKILL.md` | `SKILL/SKILL.md` (full copy) | Full staged copy with surgical edits only, language and structure kept (Russian): §4.1 table gains a `Стадия` column naming the stage or workflow per phase; §5 states that installation is `gt.py i9_install` and keeps the facts table; lessons п.12 (`sys.executable` for `.py` probes and the js quoted-literal fallback — noted in the I.9 row), п.13 (scanner exclusions — I.4 row), п.14 (component naming — I.2 row), п.15 (banned-word exclude-with-why as the I.9 move on a production-file hit), п.19 (`--allow-dirty` in I.10), п.20 (fleet-probe ETA basis) folded in. Untouched sections are copied byte for byte. |
| `references/prompts.md` | `SKILL/references/prompts.md` (full copy) | Full staged copy plus two additions: the probe-contract rule (п.13) that repo-wide scanners skip `STATUS.yaml`, `tools/ground_truth/`, `.claude/` and `docs/_human/`; and a note in the I.1 finder skeletons that the research JSON must validate against `pipeline/schemas/*.schema.json`. |
| `selftest/README.md` | `SKILL/selftest/fixture/README.md`, `make_fixture.sh`, `SKILL/selftest/run.py`, this spec | How to run the fixture init selftest: materialise the fixture, write a `run.json` with all model roles set to opus at effort low, run the stages and workflows in chain order (the main session issues the Workflow calls), then `check_expected.py`; expected wall time 10–20 minutes; how it relates to `selftest/run.py`, which proves the verifier templates rather than the pipeline. |

---

## 8. Risks

- `banned_words.IDENTITY_TEXT` must be byte-identical to every `.js` literal; the eight source
  scripts word it three slightly different ways, so G1 fixes one wording and G2–G5 paste it.
  `check_identity_sync.py` is the gate.
- `i4.js` in the scratchpad reads a compact `args.pg/tg/cn/brief_dir` while the saved
  `i4-args.json` carries the expanded shape; the port takes the expanded shape, so the prompt
  builders need the small rewrite noted in G3.
- Anchor regexes are duplicated in five scratch scripts with different extension lists;
  the merged `remap_line_refs.py` owns the one used by G4, and `i5_recon_apply` imports it
  rather than keeping a copy.
- `i9_install` is new code with no prior script; its idempotence and the settings merge are the
  parts most likely to be wrong, and the fixture selftest is the only proof available.
- Line numbers cited in prose are the one place where a wrong rewrite is silent; `assoc_risk`
  must stay a report, never an edit.
