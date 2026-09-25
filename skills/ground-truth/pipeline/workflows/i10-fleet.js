// I.10 fleet-probe (acceptance): one fresh navigator per target answers
// "where would you change X" from a cold start, in three reads or
// fewer; one grader checks every answer against the actual tools
// (blast_radius.py, STATUS.yaml), not the navigator's own confidence.
//
// args:
//   repo    - absolute path to the repository (or worktree) root
//   python  - interpreter to run this repo's checks with
//   targets - [{x, expect}], x is the navigator's starting question,
//             expect names the intended claim id plus sibling ids
//   models  - optional {role: {model, effort?}} override map; roles
//             are navigator and grader
//
// Phase: Acceptance -- one navigator per target, then one grader over
// all navigator answers together.
//
// Result: { probes, grade, ok, counts, failed }.
//   probes  - navigator answers, one per target that responded
//   grade   - { results: [{target, navigator_verdict, claim_id_correct,
//              path_correct, blast_radius_match, status_correct,
//              reads_within_3, evidence}], pass_count, routing_defects }
//   ok      - true iff every navigator and the grader returned a result
//             (a failing grade is not an infra failure -- read
//             grade.pass_count / grade.results for that)
//   counts  - { Acceptance: { ok, failed } }
//   failed  - [{label, phase, reason}] for a navigator or the grader
//             that returned nothing

export const meta = {
  name: 'ground-truth-i10-fleet',
  description: 'I.10 fleet-probe: fresh navigators per target, one grader against the actual tools',
  phases: [
    { title: 'Acceptance', detail: 'one navigator per target, then a grader over all answers' },
  ],
}

const DEFAULTS = {
  navigator: { model: 'opus' },
  grader: { model: 'opus', effort: 'high' },
}
const M = Object.assign({}, DEFAULTS, args.models || {})

const REPO = args.repo
const PY = args.python
const TARGETS = args.targets

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
Repository: ${REPO}
Hard rules:
- Read-only task: do not edit any file; never run git commit, add, rm, mv, checkout, stash or reset.
- Never print, copy or paraphrase a secret value, hostname or IP address; refer to them by file:line only.
- Python for this repo: ${PY}.
${IDENTITY}
`

const NAV_SCHEMA = {
  type: 'object',
  properties: {
    target: { type: 'string' }, claim_id: { type: 'string' }, path: { type: 'string' }, status: { type: 'string' },
    blast_radius: { type: 'array', items: { type: 'string' } },
    files_read_count: { type: 'integer' }, files_read: { type: 'array', items: { type: 'string' } },
  },
  required: ['target', 'claim_id', 'path', 'status', 'blast_radius', 'files_read_count', 'files_read'],
}

const probePrompt = (x) => `
You have no memory of any prior session on this repo. Starting point:
"I want to change ${x}." Using only what you can read in ${REPO}
(start from ${REPO}/CLAUDE.md; if the repo has none, start from
${REPO}/.claude/rules/ground-truth.md -- and follow its instructions
literally), find:
the status-contract claim id this touches, the file path it names, its
current status, and its blast radius (direct importers + declared
cross-service edges, as printed by the blast-radius tool the routing
files name).
Report exactly how many files you opened, in order, to get there. Reading a
file means seeing its contents by any tool -- a read tool, cat, sed -n, head,
tail: all of those count as one read of that file, whole or by line range.
The routing files you start from -- CLAUDE.md (if present) and the rules
file it points you to -- do not count, a grep/rg that shows only matched
lines does not count no matter how wide the context, a git command does
not count, and running a tool does not count -- list those in files_read
with a "(grep)" or "(tool)" suffix anyway; never put a cat/sed/head dump
under "(tool)". Each time you open STATUS.yaml itself, or print a range of
it, instead of grepping it, that is one read, the same as opening any
other file -- doing that for two different claim blocks is two reads,
while reading both via grep is zero. A claim whose path is a list counts as one read however many of
its files you open; open the one its note names first. If several claims
name the same path with different statuses, take the one whose concern
matches "${x}". Do not read anything outside ${REPO}. Do not edit anything.
Use ${PY} for any command. Never print secret values, hostnames or IP
addresses.
${IDENTITY}
Return JSON: {"target","claim_id","path","status","blast_radius":[...],
"files_read_count":<int>,"files_read":[...]}.
`

const GRADE_SCHEMA = {
  type: 'object',
  properties: {
    results: { type: 'array', items: { type: 'object', properties: {
      target: { type: 'string' }, navigator_verdict: { type: 'string', enum: ['pass', 'fail'] },
      claim_id_correct: { type: 'boolean' }, path_correct: { type: 'boolean' }, blast_radius_match: { type: 'boolean' }, status_correct: { type: 'boolean' },
      reads_within_3: { type: 'boolean' }, evidence: { type: 'string' } },
      required: ['target', 'navigator_verdict', 'claim_id_correct', 'path_correct', 'blast_radius_match', 'status_correct', 'reads_within_3', 'evidence'] } },
    pass_count: { type: 'integer' },
    routing_defects: { type: 'array', items: { type: 'string' } },
  },
  required: ['results', 'pass_count', 'routing_defects'],
}

const gradePrompt = (answers) => `${COMMON}
Grade these ${answers.length} navigator answers. For each target, independently run
${PY} tools/ground_truth/blast_radius.py against the path the navigator named
(cwd ${REPO}), and read the claim itself in STATUS.yaml (rg -n -A16 '^- id: <id>$' STATUS.yaml).
Compare the navigator's claim_id, path, status, and blast radius against what
you just found -- not against the navigator's own confidence or explanation.
Any mismatch is a fail for that field, regardless of how the navigator
justified it. Count reads the way the navigator was told to: entries suffixed
"(grep)" or "(tool)" and the routing files it started from (CLAUDE.md, if the
repo has one, and the rules file) do not count, a git command does not count
either, each time STATUS.yaml is opened with a read tool or printed with
cat/sed/head instead of grep is one read -- opened for two different claim
blocks is two reads, not one, while two claim blocks pulled with rg is zero
-- and a claim whose path is a list still counts as one read however many of
its files were opened for that claim -- more than three counted reads fails
reads_within_3 and the target.
Recount from the files_read list yourself: a cat/sed -n/head/tail of a file
is a read even when the navigator suffixed it "(tool)"; a wrong self-count
is a routing defect to report, not a reason to trust the number. For a multi-path claim, check the blast radius
by claim id: the union of blast_radius.py over every path that claim names
(${PY} tools/ground_truth/blast_radius.py <claim-id>), not only the one path
the navigator quoted. When several claims cover the target with different
statuses, accept the one whose id and note match the target's concern -- even
if the expected id you were handed names another one -- and say so in evidence.
The decider expected these claim ids (a hint to verify, not the truth):
${JSON.stringify(TARGETS.map(t => ({ target: t.x, expected: t.expect })))}.
Also list routing_defects: anything in CLAUDE.md or .claude/rules/ground-truth.md
that made a navigator take a wrong turn or an extra read. Empty list if none.

Navigator answers:
${JSON.stringify(answers, null, 1)}`

phase('Acceptance')
log(`I.10 fleet-probe: ${TARGETS.length} fresh navigators`)
const rawAnswers = await parallel(TARGETS.map((t, i) => () =>
  agent(probePrompt(t.x), { label: `probe:${i + 1}`, phase: 'Acceptance', ...M.navigator, schema: NAV_SCHEMA })))
const answers = rawAnswers.filter(Boolean)
log(`navigators returned ${answers.length}/${TARGETS.length}; grading`)
const grade = answers.length
  ? await agent(gradePrompt(answers), { label: 'probe:grade', phase: 'Acceptance', ...M.grader, schema: GRADE_SCHEMA })
  : null

const missingNavigators = TARGETS
  .map((_, i) => (rawAnswers[i] ? null : { label: `probe:${i + 1}`, phase: 'Acceptance', reason: 'agent returned null' }))
  .filter(Boolean)
const missingGrade = grade ? [] : [{ label: 'probe:grade', phase: 'Acceptance', reason: 'agent returned null' }]
const failed = [...missingNavigators, ...missingGrade]
const ok = failed.length === 0
const responded = answers.length + (grade ? 1 : 0)
const counts = { Acceptance: { ok: responded, failed: TARGETS.length + 1 - responded } }
log(`I.10 done: ok=${ok}, pass_count=${grade ? grade.pass_count : 0}/${TARGETS.length}`)

return { probes: answers, grade, ok, counts, failed }
