// I.4 code fix: classify code-fix briefs, then write probes, write
// characterization tests, and verify canary mutations against them.
//
// args (written by i4_build_args.py):
//   repo        - absolute path to the repository worktree
//   python      - absolute path to the python interpreter to run pytest with
//   tmp_base    - scratch directory for red-demo copies of the worktree
//   brief_dir   - base directory the per-brief JSON files live under
//   classify    - path to the JSON array of code-fix briefs to classify
//   probe_groups - [{id, brief_file, probes: [{claim_id, ..., probe_file}]}]
//   test_groups  - [{id, test_file, exists, first_nodes, brief_file, claims: [...]}]
//   canaries     - [{claim_id, mutation_suggestion, brief_file, ...}]
//   models      - optional {role: {model, effort?}} override map
//
// Roles: every phase uses M.codefix (Classify, Probes, Tests, Canaries
// are all the same agent role in this pipeline -- writing or verifying a
// small, scoped, non-production artifact under a fixed contract).
//
// D12 (production code, roles, playbooks, CI, data and docs are never
// touched in this phase) and D24 (exactly one new pytest function per
// claim needing one) are enforced by prompt instruction and by what the
// brief data hands each agent, not by anything in this script -- the
// classify labels below are informational only and never gate what a
// probe/test/canary agent is allowed to do.
//
// Result: { classes, probes, tests, canaries, ok, counts, failed }.
//   classes  - the Classify phase's entries[] (claim_id, class, prod_files,
//              self_inconsistency, summary), [] if that agent failed
//   probes   - one entry per probe group that returned a result
//   tests    - one entry per test file that returned a result
//   canaries - one entry per canary claim that returned a result
//   ok       - true when every phase produced a result for every input
//   counts   - { Classify: {ok,failed}, Probes: {...}, Tests: {...}, Canaries: {...} }
//   failed   - [{label, phase, reason}]

export const meta = {
  name: 'ground-truth-i4-code-fix',
  description: 'Phase I.4 code fix: classify briefs, write probes and characterization tests, verify canaries',
  phases: [
    { title: 'Classify', detail: 'one agent classifies every code-fix brief' },
    { title: 'Probes', detail: 'one agent per probe group' },
    { title: 'Tests', detail: 'one agent per test file, one new test per claim' },
    { title: 'Canaries', detail: 'verify a mutation makes the check red' },
  ],
}

const DEFAULTS = {
  codefix: { model: 'opus' },
}
const M = Object.assign({}, DEFAULTS, args.models || {})

const IDENTITY = `
Your output may end up committed to a pushed repository. Write plain,
neutral engineering prose, as any developer on the team would. Never
mention Claude, Anthropic, ChatGPT, GPT, Copilot, LLM, "AI",
"AI-assisted", "AI-generated", "coding agent", "language model",
"assistant", or add a Co-Authored-By line anywhere -- not in claim
text, notes, code comments, commit messages, or filenames. Avoid:
delve, leverage, comprehensive, robust, seamless, streamline,
consolidate, modernize, enhanced, utilize, facilitate.
`

const COMMON = `
Repository worktree: ${args.repo} (a git worktree; other people work in
the main tree, never touch anything outside this worktree). Interpreter for
every python/pytest invocation: ${args.python}. Run pytest as
"PYTHONDONTWRITEBYTECODE=1 ${args.python} -m pytest -q -p no:cacheprovider <node>"
with cwd = the worktree root. Other agents work in the same worktree at the
same time, so: never run git commands that change state (no add, commit,
stash, checkout, restore, clean, reset); never edit or temporarily mutate a
tracked file that is not in your allowed list; never touch files owned by
other agents. No network access of any kind. Temporary copies go under
${args.tmp_base}/<your-label>/ and are deleted when you finish.

Showing a check red without touching the real worktree: copy the worktree
with "rsync -a --exclude=.git --exclude=.venv --exclude=__pycache__ ${args.repo}/ <tmp>/",
apply the breaking edit inside <tmp> only, run the check from <tmp> as cwd
(imports resolve to the copy because the package is picked up from cwd),
observe the failure, then delete <tmp>. The real worktree stays untouched.

Rules from the contract (D12): production code, roles, playbooks, CI, data
and docs are never changed in this phase. If a brief asks for such a change,
skip that part and report it under left_out. A stub body (bare
NotImplementedError, empty function) is never implemented. Set
forbidden_action_taken=true only if you did something the rules forbid;
never do it and claim false.
${IDENTITY}`

const PROBE_RULES = `Probe contract, every probe file:
- first line "#!/usr/bin/env python3", then "chmod +x" the file;
- REPO = Path(__file__).resolve().parents[3] (the file lives at
  tools/ground_truth/probes/<name>.py); never depend on cwd;
- stdlib only, plus repo modules imported after sys.path.insert(0, str(REPO))
  when the brief needs them; no third-party packages beyond what the repo
  already installs; no network; no writes anywhere except /tmp-style
  tempfile dirs it creates and removes;
- exit 0 = the claim holds (print one "ok: ..." line), exit 1 = the claim is
  violated (print one "fail: ..." line naming what differed), exit 77 =
  a prerequisite is missing (print "skip: ..."): use 77 only for a missing
  interpreter/tool/optional file, never to paper over a failing assertion;
- must finish in under 20 seconds on this machine; a probe that runs a
  pytest subset uses the current interpreter (sys.executable),
  "-p no:cacheprovider" and PYTHONDONTWRITEBYTECODE=1 and limits itself to
  the node ids the brief names, never the whole suite unless the brief says
  so, and then with a 50 second subprocess timeout;
- a module docstring of 2-6 lines saying which STATUS.yaml claim id it backs
  and what "fail" means, nothing more; plain engineering prose;
- the probe must be shown red once: in a temp copy of the worktree break
  exactly the thing the probe asserts (edit the target line, remove the
  file, rename the job, ...), run the COPIED probe file from the copy, and
  record exit 1 and its fail line. A probe that stays green under that
  breakage is not finished -- fix the probe, not the breakage.`

const CLASS_SCHEMA = {
  type: 'object',
  properties: {
    entries: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          claim_id: { type: 'string' },
          class: { type: 'string', enum: ['test_only', 'probe_only', 'mixed', 'prod_change'] },
          prod_files: { type: 'array', items: { type: 'string' } },
          self_inconsistency: { type: 'boolean' },
          summary: { type: 'string' },
        },
        required: ['claim_id', 'class', 'prod_files', 'self_inconsistency', 'summary'],
      },
    },
  },
  required: ['entries'],
}

const PROBE_SCHEMA = {
  type: 'object',
  properties: {
    group: { type: 'string' },
    results: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          claim_id: { type: 'string' },
          probe_file: { type: 'string' },
          written: { type: 'boolean' },
          exit_at_head: { type: 'integer' },
          head_line: { type: 'string' },
          runtime_seconds: { type: 'number' },
          red_demo_mutation: { type: 'string' },
          red_demo_exit: { type: 'integer' },
          red_demo_line: { type: 'string' },
          left_out: { type: 'array', items: { type: 'string' } },
          notes: { type: 'string' },
        },
        required: ['claim_id', 'probe_file', 'written', 'exit_at_head', 'red_demo_mutation', 'red_demo_exit'],
      },
    },
    files_written: { type: 'array', items: { type: 'string' } },
    forbidden_action_taken: { type: 'boolean' },
  },
  required: ['group', 'results', 'files_written', 'forbidden_action_taken'],
}

const TEST_SCHEMA = {
  type: 'object',
  properties: {
    test_file: { type: 'string' },
    results: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          claim_id: { type: 'string' },
          node_id: { type: 'string' },
          written: { type: 'boolean' },
          red_demo_file: { type: 'string' },
          red_demo_find: { type: 'string' },
          red_demo_replace: { type: 'string' },
          red_demo_failure: { type: 'string' },
          green_at_head: { type: 'boolean' },
          left_out: { type: 'array', items: { type: 'string' } },
          notes: { type: 'string' },
        },
        required: ['claim_id', 'node_id', 'written', 'red_demo_file', 'red_demo_find', 'red_demo_replace', 'red_demo_failure', 'green_at_head', 'left_out'],
      },
    },
    file_green: { type: 'boolean' },
    file_test_count: { type: 'integer' },
    forbidden_action_taken: { type: 'boolean' },
  },
  required: ['test_file', 'results', 'file_green', 'forbidden_action_taken'],
}

const CANARY_SCHEMA = {
  type: 'object',
  properties: {
    claim_id: { type: 'string' },
    mutation_file: { type: 'string' },
    mutation_find: { type: 'string' },
    mutation_replace: { type: 'string' },
    red_nodes: { type: 'array', items: { type: 'string' } },
    red_failure: { type: 'string' },
    green_at_head: { type: 'boolean' },
    suggestion_was_toothless: { type: 'boolean' },
    notes: { type: 'string' },
  },
  required: ['claim_id', 'mutation_file', 'mutation_find', 'mutation_replace', 'red_nodes', 'red_failure', 'green_at_head', 'suggestion_was_toothless'],
}

const BRIEF_FIELDS = `Each claim record has: claim_id, component, status, path (list), check
(the check string STATUS.yaml will carry), note (what the claim says about
the code), brief (what the judge decided must be written -- implement this,
not more), judge_reason, was_red_plan (hint for the red demo).`

const briefRef = (file, what) => `Your brief file: ${file} -- read it first with the Read tool; it is a JSON
${what}. ${BRIEF_FIELDS}`

const classifyPrompt = `${IDENTITY}

${briefRef(args.classify, 'array, one record per code-fix claim')}

For each record, decide:
- class: "test_only" (the brief only needs a new test, no production code
  change is implied by the claim itself), "probe_only" (same, but the
  check is a probe rather than a pytest node), "mixed" (the brief reads as
  wanting both a test/probe and a production change), or "prod_change"
  (the brief is really asking for a production code change, with a test
  secondary or absent).
- prod_files: the production file path(s), if any, the brief's own wording
  points at as needing a change (empty array if none).
- self_inconsistency: true if the claim's own fields contradict each other
  (for example the note says the behavior already works but the brief asks
  for a fix, or status is "absent" but the brief describes fixing existing
  behavior).
- summary: one plain sentence.

This classification is informational only: I.4 never edits production
code, roles, playbooks, CI, data or docs regardless of the class you
assign (D12) -- classify what the brief asks for, not what will be done
about it.

Return the JSON described by the schema: one entries[] record per claim in
the file, same claim_id values, same order.`

const probePrompt = (g) => {
  const probeFiles = g.probes.map(p => p.probe_file)
  return `${COMMON}

Task: write ${probeFiles.length} probe script(s) under tools/ground_truth/probes/ in the
worktree. Allowed files: exactly ${probeFiles.join(', ')}.
Create nothing else; the probes directory already exists.

${PROBE_RULES}

Each probe backs one STATUS.yaml claim. The brief is what the judge decided
the probe must assert; the note and path say what the claim is about.
Implement the brief, not more. If the brief also asks for a test or a
production change, skip that part and report it under left_out.

${briefRef(g.brief_file, 'list with one record per probe; the record field probe_file names the file to write')}

Label for your temp dir: ${g.id}. Return the JSON described by the schema:
one results entry per probe with the exit code at HEAD, the mutation used
to show it red, the exit code and fail line observed in the copy, runtime
seconds, and files_written listing every file you created.`
}

const testPrompt = (t) => `${COMMON}

Task: characterization tests in ONE file: ${t.test_file}${t.exists ? ' (exists; keep its style, imports and fixtures; append, do not reorder)' : ' (does not exist yet; create it in the style of its sibling test files in the same directory)'}.
Allowed files: exactly that test file. Nothing else in the worktree may change.

Budget rule (D24): exactly ONE new test function per claim below, placed at
the first node id listed under missing_nodes (create the class if the node
names one; if the brief's shape makes a different single node the right
one, use it and report it as node_id). node_id is the id exactly as
pytest collects it from the file you wrote: path::TestClass::test_name
when the test sits in a class, path::test_name only for a module-level
function. If the brief describes several
tests, write the one that pins the claim's core assertion and list the
others under left_out as one sentence each, so they can be filed as debt.
No helper changes to production code. A test for an "absent" or "partial"
status claim pins today's behaviour (it asserts that the missing thing is
missing), it never adds the missing thing.

TDD proof: for each new test, copy the worktree to a temp dir, apply the
smallest production edit there that the new test must catch (start from
the red plan hint), run just that node id from the copy and record the
assertion it fails on. Then run the node id in the real worktree: it must
pass. If the test cannot be made to fail by any edit of the code it is
supposed to pin, it is not a characterization test -- rewrite it.
Finally run the whole test file in the real worktree once and report
whether it is green and how many tests it collects.

Style: mirror the file (unittest or pytest style, same fixture idioms),
no new dependencies, tempfile for any disk state, no network, no sleeping,
each test well under one second. Plain engineering docstrings, one line.

${briefRef(t.brief_file, 'object with a claims list, one record per claim; the record field missing_nodes lists the node ids the check expects, write the first one')}
First nodes to write: ${t.first_nodes.join(', ')}.

Label for your temp dir: ${t.id}. Return
the JSON described by the schema.`

const canaryPrompt = (c) => `${COMMON}

Task: verify a canary mutation for one claim. You edit nothing in the real
worktree (allowed files: none). Work entirely in a temp copy.

${briefRef(c.brief_file, 'object for one claim; its mutation_suggestion field is "file | find | replace"')}
Claim: ${c.claim_id}; its check node ids are in the brief file.
Suggested mutation (file | find | replace): ${c.mutation_suggestion}

Steps: (1) run the claim's check node ids in the real worktree, they must
pass at HEAD; (2) copy the worktree to a temp dir, apply the suggested
mutation there (find must be an exact, unique substring of the file), run
the same node ids from the copy; (3) if at least one node fails on its own
assertion, record it; if all stay green the suggestion is toothless -- pick
a different one-line mutation in the same production file that breaks the
exact value or branch the check reads (not an import, comment or
neighbouring line), and prove it red the same way; (4) delete the temp dir.
Return the JSON described by the schema with the final mutation (file
relative to the worktree root, exact find and replace strings), the node
ids that went red and the assertion text.

Label for your temp dir: canary_${c.claim_id}.`

phase('Classify')
log(`I.4: ${args.probe_groups.length} probe groups, ${args.test_groups.length} test files, ${args.canaries.length} canaries`)

const cls = await agent(classifyPrompt, { label: 'classify', phase: 'Classify', schema: CLASS_SCHEMA, ...M.codefix })

// Probes, Tests and Canaries are independent groups -- each agent() call
// carries its own phase for progress grouping, so the three pipelines run
// concurrently under one barrier instead of one after another.
const probes = pipeline(args.probe_groups, g => agent(probePrompt(g), { label: `probe:${g.id}`, phase: 'Probes', schema: PROBE_SCHEMA, ...M.codefix }))
const tests = pipeline(args.test_groups, tg => agent(testPrompt(tg), { label: `test:${tg.test_file}`, phase: 'Tests', schema: TEST_SCHEMA, ...M.codefix }))
const canaries = pipeline(args.canaries, cg => agent(canaryPrompt(cg), { label: `canary:${cg.claim_id}`, phase: 'Canaries', schema: CANARY_SCHEMA, ...M.codefix }))
const [p, t, c] = await Promise.all([probes, tests, canaries])

const pn = p.filter(Boolean), tn = t.filter(Boolean), cn = c.filter(Boolean)

const failed = [
  cls ? null : { label: 'classify', phase: 'Classify', reason: 'agent returned null' },
  ...args.probe_groups.map((g, i) => (p[i] ? null : { label: `probe:${g.id}`, phase: 'Probes', reason: 'agent returned null' })),
  ...args.test_groups.map((g, i) => (t[i] ? null : { label: `test:${g.test_file}`, phase: 'Tests', reason: 'agent returned null' })),
  ...args.canaries.map((g, i) => (c[i] ? null : { label: `canary:${g.claim_id}`, phase: 'Canaries', reason: 'agent returned null' })),
].filter(Boolean)

const counts = {
  Classify: { ok: cls ? 1 : 0, failed: cls ? 0 : 1 },
  Probes: { ok: pn.length, failed: p.length - pn.length },
  Tests: { ok: tn.length, failed: t.length - tn.length },
  Canaries: { ok: cn.length, failed: c.length - cn.length },
}

log(`I.4 done: classify ${cls ? 1 : 0}/1, probes ${pn.length}/${p.length}, tests ${tn.length}/${t.length}, canaries ${cn.length}/${c.length}`)

return {
  classes: cls ? cls.entries : [],
  probes: pn,
  tests: tn,
  canaries: cn,
  ok: failed.length === 0,
  counts,
  failed,
}
