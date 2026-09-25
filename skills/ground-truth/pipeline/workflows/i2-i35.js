// I.2 reconcile / I.3 adversarial verify / I.3.5 judge synthesis: turns
// each component slice into judged status-contract claims.
//
// args:
//   repo      - absolute path to the repository (or worktree) root
//   research  - absolute path to the I.1 research directory
//   slices    - [{component, slice_path, root, entry_point, language}],
//               one entry per component (from slices-index.json)
//   python    - interpreter to run the repo's own tests with (the repo's
//               venv, not the system python3)
//   models    - optional {role: {model, effort?}} override map; roles
//               are reconcile, defender, prosecutor, judge
//
// Phases: Reconcile (one drafter per component) -> Adversarial (a
// defender on every draft, a prosecutor on status-raising ones only) ->
// Judge (batches of judged claims).
//
// Result: { items, judged, batch_defects, failed_components, failed_batches,
//           routes, ok, counts, failed }.
//   items             - every draft claim plus its adversarial votes
//   judged            - flattened judge results across all batches (verdict
//                        key names: final_status, final_check, final_path,
//                        final_note, merged_into, code_fix_brief, route, ...)
//   batch_defects     - cross-claim defects a judge batch merged or resolved
//   failed_components - components whose reconcile agent returned null
//   failed_batches    - [claim_id, ...] for judge batches that returned null
//   routes            - { CODE_FIX_CANDIDATE|DOC_FIX_ONLY|ESCALATE: count }
//   ok                - true when nothing failed in any phase
//   counts            - { Reconcile: {ok, failed}, Adversarial: {ok, failed},
//                          Judge: {ok, failed} }
//   failed            - [{label, phase, reason}]

export const meta = {
  name: 'ground-truth-i2-i35',
  description: 'Phases I.2 reconcile, I.3 adversarial verify, I.3.5 judge synthesis',
  phases: [
    { title: 'Reconcile', detail: 'one drafter per component' },
    { title: 'Adversarial', detail: 'per component: defender on all drafts + prosecutor on status-raising ones' },
    { title: 'Judge', detail: 'batches of 5-8 claims' },
  ],
}

const DEFAULTS = {
  reconcile: { model: 'opus' },
  defender: { model: 'opus', effort: 'high' },
  prosecutor: { model: 'opus', effort: 'high' },
  judge: { model: 'opus', effort: 'xhigh' },
}
const M = Object.assign({}, DEFAULTS, args.models || {})

const REPO = args.repo
const RESEARCH = args.research
const SLICES = args.slices   // [{component, slice_path, root, entry_point, language}]
const PY = args.python

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

const SENSITIVE = `
This repository may hold infrastructure or credential-adjacent material.
Never copy IP addresses, hostnames, domain names, SSH users,
credentials, tokens, vault contents or key material into your output or
into any file you write. Refer to such values by file:line plus a
generic description ("a host entry", "a vault variable"). If a file is
clearly encrypted or sealed (a vault file, a sealed secret), do not try
to decrypt it. Do not run anything that touches the network (no deploy,
no ssh, no curl). Read-only on the repository: do not edit, create or
delete files under it. The only files you may write are the output
files named below.
`

const COMMON = `
Repository root: ${REPO}
Work only inside this path.
When you need to see a check pass or fail, run it read-only through
its own test runner ("${PY} -m pytest -p no:cacheprovider -q <nodeid>"
for a pytest nodeid, the matching command for a go/js test); never edit
the repository to make a check pass.
${SENSITIVE}
${IDENTITY}
`

const SCHEMA_RULES = `
Status contract schema (STATUS.yaml v1), the parts you need:
- kind is exactly one of: status | live_state | out_of_repo.
- kind=status carries: status (implemented | partial | stub | absent |
  design-only), path (file or list of files, relative to repo root, no
  line suffix), check_kind (pytest | go_test | js_test | junit | probe), check.
  Every path must exist in the tree today (the verifier fails a missing
  path in every mode, absent claims included): for status absent or
  design-only, path is where the behavior is promised or would have to
  be registered (the doc that describes it, the registry or CI file that
  lacks it), never the missing file itself.
  status must be PROVEN by check right now, never by intent.
- check syntax: pytest -> "path/test_x.py::TestClass::test_method" or
  "path::test_function", several joined by ";"; go_test ->
  "pkg/path::TestName"; js_test -> "path/test_file.js::<exact test
  name string>"; junit -> "fqn.Class::method", several joined by ";";
  probe -> "tools/ground_truth/probes/<name>.py" (a script
  to be written in a later phase, exit 0 holds / 1 broken / 77 skip).
  A probe is the right check for non-code units with no test of their
  own -- a config file, a CI job, a generated artifact, a data file:
  describe in check_draft exactly what the probe must assert (e.g.
  "config X sets field Y; CI job Z runs step W that depends on it").
- kind=live_state carries: command (how to obtain today's value: a
  grep of a deploy config, a CLI call) -- never a stored value; no
  status/check. path optional.
- kind=out_of_repo carries: pointer (where the subject really lives);
  no status/check/command.
- note is mandatory, grounded in code (path plus function/class/section
  name), never a doc line reference. note is a YAML plain scalar: no
  ": " sequence inside it -- use ";" or ",".
- id: snake_case, unique, stable, names the CONCERN (behavior), not the
  file. One file may carry several claims with different statuses when it
  mixes concerns; two claims with the same path AND the same check but
  different status are a defect.
- Granularity: service/package level; a group of similar one-off files
  with no normative claim of its own = one claim with path as a list and
  one probe; dead code = implemented + a debt candidate "unreferenced".
`

const DRAFT_SCHEMA = {
  type: 'object',
  properties: {
    component: { type: 'string' },
    claims: { type: 'array', items: { type: 'object', properties: {
      id: { type: 'string' }, component: { type: 'string' },
      kind: { type: 'string', enum: ['status', 'live_state', 'out_of_repo'] },
      status: { type: 'string', enum: ['implemented', 'partial', 'stub', 'absent', 'design-only', 'n/a'] },
      path: { type: 'array', items: { type: 'string' } },
      check_kind: { type: 'string', enum: ['pytest', 'go_test', 'js_test', 'junit', 'probe', 'n/a'] },
      check_draft: { type: 'string' },
      command: { type: 'string' },
      pointer: { type: 'string' },
      note: { type: 'string' },
      evidence_for: { type: 'array', items: { type: 'string' } },
      evidence_against: { type: 'array', items: { type: 'string' } },
      was_check_ever_red_plan: { type: 'string' },
      check_exists_now: { type: 'boolean' },
      debt_candidates: { type: 'array', items: { type: 'object', properties: {
        path: { type: 'string' }, description: { type: 'string' }, severity: { type: 'string', enum: ['blocking', 'normal', 'cosmetic'] },
        evidence: { type: 'string' } }, required: ['path', 'description', 'severity', 'evidence'] } },
      edge_candidates: { type: 'array', items: { type: 'string' } },
    }, required: ['id', 'component', 'kind', 'status', 'path', 'check_kind', 'check_draft', 'note', 'evidence_for', 'evidence_against', 'was_check_ever_red_plan', 'check_exists_now', 'debt_candidates', 'edge_candidates'] } },
    doc_conflicts: { type: 'array', items: { type: 'string' } },
  },
  required: ['component', 'claims', 'doc_conflicts'],
}

const PROSECUTOR_SCHEMA = {
  type: 'object',
  properties: {
    claim_id: { type: 'string' },
    verdict: { type: 'string', enum: ['challenge', 'accept'] },
    findings: { type: 'array', items: { type: 'string' } },
    counter_evidence: { type: 'array', items: { type: 'string' } },
    proposed_status: { type: 'string' },
    proposed_check: { type: 'string' },
  },
  required: ['claim_id', 'verdict', 'findings', 'counter_evidence', 'proposed_status', 'proposed_check'],
}

const DEFENDER_SCHEMA = {
  type: 'object',
  properties: {
    claim_id: { type: 'string' },
    verdict: { type: 'string', enum: ['accept', 'revise'] },
    rebuttal_or_confirmation: { type: 'array', items: { type: 'string' } },
    proposed_status: { type: 'string' },
    proposed_check: { type: 'string' },
  },
  required: ['claim_id', 'verdict', 'rebuttal_or_confirmation', 'proposed_status', 'proposed_check'],
}

const PROSECUTOR_BATCH_SCHEMA = { type: 'object', properties: { verdicts: { type: 'array', items: PROSECUTOR_SCHEMA } }, required: ['verdicts'] }
const DEFENDER_BATCH_SCHEMA = { type: 'object', properties: { verdicts: { type: 'array', items: DEFENDER_SCHEMA } }, required: ['verdicts'] }

const JUDGE_SCHEMA = {
  type: 'object',
  properties: {
    results: { type: 'array', items: { type: 'object', properties: {
      claim_id: { type: 'string' },
      merged_into: { type: 'string' },
      final_kind: { type: 'string', enum: ['status', 'live_state', 'out_of_repo'] },
      final_status: { type: 'string' },
      final_path: { type: 'array', items: { type: 'string' } },
      final_check_kind: { type: 'string' },
      final_check: { type: 'string' },
      final_command: { type: 'string' },
      final_pointer: { type: 'string' },
      final_note: { type: 'string' },
      route: { type: 'string', enum: ['CODE_FIX_CANDIDATE', 'DOC_FIX_ONLY', 'ESCALATE'] },
      reason: { type: 'string' },
      code_fix_brief: { type: 'string' },
      debt: { type: 'array', items: { type: 'object', properties: {
        path: { type: 'string' }, description: { type: 'string' }, severity: { type: 'string', enum: ['blocking', 'normal', 'cosmetic'] } },
        required: ['path', 'description', 'severity'] } },
      canary_candidate: { type: 'boolean' },
      mutation_suggestion: { type: 'string' },
    }, required: ['claim_id', 'merged_into', 'final_kind', 'final_status', 'final_path', 'final_check_kind', 'final_check', 'final_command', 'final_pointer', 'final_note', 'route', 'reason', 'code_fix_brief', 'debt', 'canary_candidate', 'mutation_suggestion'] } },
    batch_defects_fixed: { type: 'array', items: { type: 'string' } },
  },
  required: ['results', 'batch_defects_fixed'],
}

// A slice whose items did not map to any codemap component lands in an
// area bucket named after the path itself (e.g. "app/core"), written to
// slices/unmapped-<slug>.json -- detect it by that filename, not by the
// component field, since the component field is a normal-looking path
// and must stay that way for anything downstream that treats it as a
// short identifier.
function isAreaBucket(s) {
  return /\/unmapped-[^/]+\.json$/.test(s.slice_path)
}

function reconcilePrompt(s) {
  const bucketNote = isAreaBucket(s)
    ? `NOTE: this slice is an AREA BUCKET, not one component: its items did not map
to any code-map component. Read the code under the area, draft claims only for
what the items really refer to; if an item is already covered by an obvious
sibling component (same file/function), say so in doc_conflicts and skip it
rather than drafting a duplicate.
`
    : ''
  return `
${bucketNote}You have doc claims, a code map entry, and debt suspects for exactly one
component: "${s.component}" (root ${s.root}, entry point ${s.entry_point},
language ${s.language}). The slice is at ${s.slice_path} (JSON with keys
codemap, claims, debt_suspects). The full research files are in
${RESEARCH}/ (claims.json, codemap-*.json, debt-candidates.json,
inventory.json) if the slice looks incomplete.

Draft the status-contract claim(s) for this component: usually one; two
or three only when the component mixes concerns with different statuses
(a live path and a stub next to it, a script and its missing guard).
For each claim: id, kind, status (or "n/a" for non-status kinds), path
(list, even for a single file), check_kind + check_draft (an existing
test nodeid if one really asserts this behavior -- open it and confirm
it calls the code and asserts on the result -- else a proposed test or
probe, described precisely), command (live_state) or pointer
(out_of_repo) or "" when not applicable, a note grounded in code, not
intent. List evidence_for/evidence_against separately -- do not resolve
disagreement, that is the adversarial pass's job. State exactly what
mutation would make the check go red (was_check_ever_red_plan); if none
occurs to you, say so explicitly. check_exists_now = true only if the
check_draft points at a test that exists at HEAD. debt_candidates:
anything in the slice's debt_suspects that survives a look at the code
(TODO with real missing behavior, disabled test, a key-looking literal:
report file:line and kind, never the value), plus dead code
("unreferenced, delete candidate", normal). Each entry carries path:
the file (file:line when the suspect has one) the entry is about; a
suspect taken from a doc keeps the doc's path, not the code it
describes, since that doc line is what has to change. edge_candidates: one line
each for a dependency on another component or another repo that a
later phase should turn into a probed edge. doc_conflicts: one line per
place where a doc in the slice says something the code does not do.
${SCHEMA_RULES}
${COMMON}
`
}

function prosecutorPrompt(claims, component) {
  return `
Attack these ${claims.length} draft claim(s) of component "${component}",
one verdict per claim_id, every claim_id present in your output:
${JSON.stringify(claims, null, 1)}
Judge each claim on its own; sharing the file reads between them is
fine, sharing a verdict is not. Your job is to find reasons the claimed
status is wrong or the check is too weak to catch a regression -- not
to be balanced. Read the actual
files in path and the actual check (if check_exists_now, open the test;
if it is a proposed probe/test, judge whether the description would
really fail when the behavior is deleted). Common failure modes to
specifically rule out: the check asserts the function exists but never
calls it; the status was raised on the strength of docs, not code; the
check would pass even if the described behavior were deleted; the path
list hides a file whose real state differs; a stub or no-op is dressed
as implemented. Verdict is "challenge" (claim as drafted does not hold)
or "accept" (you tried and could not break it). Fill proposed_status and
proposed_check with what you would put instead ("" if accepted as is).
${SCHEMA_RULES}
${COMMON}
`
}

function defenderPrompt(claims, component) {
  return `
Independently evaluate these ${claims.length} draft claim(s) of component
"${component}", one verdict per claim_id, every claim_id present in your
output:
${JSON.stringify(claims, null, 1)}
Judge each claim on its own; sharing the file reads between them is
fine, sharing a verdict is not. You do not see any other agent's verdict
on them. Read the files in path
and the check yourself (open the test if check_exists_now; otherwise
judge the proposed check's description). Verdict is "accept" (status and
check hold up) or "revise" (say exactly what should change: status,
check, kind, or path, with the code line that proves it). Fill
proposed_status and proposed_check with your version ("" if accepted).
${SCHEMA_RULES}
${COMMON}
`
}

function judgePrompt(batch, idx) {
  return `
You are the judge for batch ${idx + 1}: ${batch.length} draft claims,
each with reconcile evidence and one or two independent adversarial
verdicts (prosecutor and defender did not see each other). Material:
${JSON.stringify(batch, null, 1)}

For each claim decide final kind, status, path, check_kind, check (a
concrete nodeid or probe path; for a probe not yet written, keep the
path tools/ground_truth/probes/<snake_name>.py and put what it must
assert into final_note or code_fix_brief), command/pointer where the
kind needs them, a final note, and route to exactly one of
CODE_FIX_CANDIDATE (code is broken relative to itself: red test, dead
wiring, a test or probe missing under a check the claim needs -- writing
a characterization test or a probe for an existing behavior IS this
route), DOC_FIX_ONLY (code is fine or honestly a stub, only prose
overclaimed or nothing to change), or ESCALATE (you cannot resolve the
disagreement with what you were given). Never route to CODE_FIX_CANDIDATE
to make a stub "real" -- that is the owner's decision, not this
pipeline's; put such cases as a debt entry with severity by impact.
When you disagree with both adversarial voices, say so in reason and
decide anyway; do not drop a claim silently. Read the code yourself when
the votes conflict.
Before returning, check the batch against itself: two claims naming the
same path and the same check but a different status are a defect --
merge them (set merged_into on the absorbed one to the surviving id and
still return it) or make each note state why that check reads
differently for the other. Also merge claims that are the same concern
under two ids. merged_into is the empty string on every claim that
survives, including the survivor of a merge; only an absorbed claim
carries an id there, and never its own -- a claim pointing at itself is
dropped downstream. Mark canary_candidate=true for at most one claim in the
batch that has an existing pytest check and a one-line constant or
condition whose mutation (mutation_suggestion: "file | find | replace")
would make that test fail. debt: the surviving debt entries for this
claim (path, description, severity), including key-looking literals
(file:line and kind only), dead code, disabled tests, stubs described as
live; path is the file (file:line when known) the entry is about, kept
from the draft's debt_candidates -- a doc-borne entry keeps the doc path.
final_note: YAML plain scalar, no ": " inside, cites code anchors, never
a doc line reference.
${SCHEMA_RULES}
${COMMON}
`
}

const RAISING = new Set(['implemented', 'partial'])

phase('Reconcile')
log(`I.2 reconcile: ${SLICES.length} components`)
const perComponent = await pipeline(
  SLICES,
  s => agent(reconcilePrompt(s), { label: `reconcile:${s.component}`, phase: 'Reconcile', ...M.reconcile, schema: DRAFT_SCHEMA }),
  async (draft, s) => {
    if (!draft || !draft.claims.length) return null
    const raising = draft.claims.filter(c => c.kind === 'status' && RAISING.has(c.status))
    const votes = await parallel([
      () => agent(defenderPrompt(draft.claims, s.component), { label: `defend:${s.component}`, phase: 'Adversarial', ...M.defender, schema: DEFENDER_BATCH_SCHEMA }),
      ...(raising.length ? [() => agent(prosecutorPrompt(raising, s.component), { label: `prosecute:${s.component}`, phase: 'Adversarial', ...M.prosecutor, schema: PROSECUTOR_BATCH_SCHEMA })] : []),
    ])
    const byId = (v) => Object.fromEntries(((v && v.verdicts) || []).map(x => [x.claim_id, x]))
    const def = byId(votes[0]), pro = byId(votes[1])
    return draft.claims.map(c => {
      const needsThree = raising.includes(c)
      return { draft: c, three_vote: needsThree, defender: def[c.id] || null, prosecutor: needsThree ? (pro[c.id] || null) : null, doc_conflicts: draft.doc_conflicts, component: s.component }
    })
  },
)

const items = perComponent.filter(Boolean).flat()
const missingVotes = items.filter(i => !i.defender || (i.three_vote && !i.prosecutor)).map(i => i.draft.id)
if (missingVotes.length) log(`adversarial verdict missing for ${missingVotes.length} claim(s): ${missingVotes.join(', ')}`)
const failedComponents = SLICES.filter((s, i) => !perComponent[i]).map(s => s.component)
log(`I.3 done: ${items.length} draft claims, ${items.filter(i => i.three_vote).length} with 3-vote; failed components: ${failedComponents.join(', ') || 'none'}`)

phase('Judge')
const BATCH = 6
const batches = []
for (let i = 0; i < items.length; i += BATCH) batches.push(items.slice(i, i + BATCH))
// avoid a trailing batch of 1-2: fold it into the previous one
if (batches.length > 1 && batches[batches.length - 1].length < 3) {
  const tail = batches.pop()
  batches[batches.length - 1].push(...tail)
}
log(`I.3.5 judge: ${batches.length} batches`)
const judged = await parallel(batches.map((b, idx) => () =>
  agent(judgePrompt(b, idx), { label: `judge:batch${idx + 1}`, phase: 'Judge', ...M.judge, schema: JUDGE_SCHEMA })))

const results = judged.filter(Boolean).flatMap(j => j.results)
const failedBatches = batches.map((b, i) => judged[i] ? null : b.map(x => x.draft.id)).filter(Boolean)
const routes = results.reduce((m, r) => { m[r.route] = (m[r.route] || 0) + 1; return m }, {})
log(`I.3.5 done: ${results.length} judged; routes ${JSON.stringify(routes)}; failed batches: ${failedBatches.length}`)

const reconcileFailed = failedComponents.map(c => ({ label: `reconcile:${c}`, phase: 'Reconcile', reason: 'agent returned null or drafted no claims' }))
const adversarialFailed = missingVotes.map(id => ({ label: id, phase: 'Adversarial', reason: 'adversarial verdict missing' }))
const judgeFailed = batches
  .map((b, i) => (judged[i] ? null : { label: `judge:batch${i + 1}`, phase: 'Judge', reason: 'agent returned null' }))
  .filter(Boolean)
const failed = [...reconcileFailed, ...adversarialFailed, ...judgeFailed]
const counts = {
  Reconcile: { ok: SLICES.length - failedComponents.length, failed: failedComponents.length },
  Adversarial: { ok: items.length - missingVotes.length, failed: missingVotes.length },
  Judge: { ok: judged.filter(Boolean).length, failed: judgeFailed.length },
}
return { items, judged: results, batch_defects: judged.filter(Boolean).flatMap(j => j.batch_defects_fixed), failed_components: failedComponents, failed_batches: failedBatches, routes, ok: failed.length === 0, counts, failed }
