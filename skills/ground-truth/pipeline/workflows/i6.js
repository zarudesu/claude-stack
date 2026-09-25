// I.6 mapping audit: two independent auditors check that no pre-edit doc
// statement was lost without a reason once this run's doc edits landed.
//
// args:
//   repo        - absolute path to the repository (or worktree) root
//   python      - interpreter to run this repo's checks with
//   sample_path - JSON file of a fixed sample: [{old_location,
//                 old_claim_quote, normative_claim_text, code_area_hint}]
//   out_dir     - directory to write audit-A.json / audit-B.json into
//   docs        - every doc in scope (repo-relative paths)
//   models      - optional {role: {model, effort?}} override map; role
//                 is "audit" (both auditors use it)
//
// Phase: Audit -- auditor A works the fixed sample from sample_path;
// auditor B independently self-samples 15-20 statements from the
// pre-edit text of the same docs. Neither sees the other's work.
//
// Result: { audits, ok, counts, failed }.
//   audits  - [{auditor, verdict, default_fail_count, real_losses,
//              sampled, verdicts: ["<old_location> -> <verdict>", ...]}]
//   ok      - true iff both auditors responded (a FAIL verdict is not an
//             infra failure -- read audits[].verdict for that)
//   counts  - { Audit: { ok, failed } }
//   failed  - [{label, phase, reason}] for an auditor that returned nothing

export const meta = {
  name: 'ground-truth-i6',
  description: 'I.6 mapping audit: two independent auditors over pre-edit doc statements',
  phases: [
    { title: 'Audit', detail: 'auditor A on the fixed sample, auditor B self-samples the pre-edit docs' },
  ],
}

const DEFAULTS = {
  audit: { model: 'opus', effort: 'high' },
}
const M = Object.assign({}, DEFAULTS, args.models || {})

const REPO = args.repo
const PY = args.python
const SAMPLE_PATH = args.sample_path
const OUT = args.out_dir
const DOCS = args.docs

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
credentials, tokens, vault contents or key material into your output.
Refer to such values by file:line plus a generic description ("a host
entry", "a vault variable"). If a file is clearly encrypted or sealed,
do not try to decrypt it. Do not run anything that touches the network.
Read-only on the repository: do not edit, create or delete any file in
it, and never run git commit, add, rm, mv, checkout, stash or reset --
the only files you write are the audit JSON file named below.
`

const SCOPE = `
Scope note: a statement that sits outside the documents this run
touched (another document, a code string, a config value) is reported
with an "out-of-scope:" prefix in target_or_reason and does not count
as a FAIL of this round. A status restated in prose had to be replaced
by or followed by a STATUS.yaml#<claim_id> reference -- that is what
grounds a "link" verdict below.
`

const COMMON = `
Repository root: ${REPO}. Python for this repo: ${PY}.
Pre-edit text of a document: "git -C ${REPO} show HEAD:<doc>" (HEAD is
the commit before this run's edits landed). Current text: the file on
disk right now.
The status contract lives at ${REPO}/STATUS.yaml -- if it is large,
grep it (rg -n '^- id: ' STATUS.yaml; rg -n -A14 '^- id: <id>$'
STATUS.yaml) rather than opening it whole.
${SCOPE}
${SENSITIVE}
${IDENTITY}
`

const AUDIT_SCHEMA = {
  type: 'object',
  properties: {
    auditor: { type: 'string' },
    sampled: { type: 'array', items: { type: 'object', properties: {
      old_claim_quote: { type: 'string' }, old_location: { type: 'string' },
      verdict: { type: 'string' }, target_or_reason: { type: 'string' } },
      required: ['old_claim_quote', 'old_location', 'verdict', 'target_or_reason'] } },
    default_fail_count: { type: 'integer' },
    real_losses: { type: 'array', items: { type: 'string' } },
    verdict: { type: 'string', enum: ['PASS', 'FAIL'] },
  },
  required: ['auditor', 'sampled', 'default_fail_count', 'real_losses', 'verdict'],
}

const METHOD = `
For each statement, find where it now lives by reading the actual
current files, STATUS.yaml and the code -- never a mapping table or a
brief someone else wrote. Verdict per statement, exactly one of:
"keep-inline" (still stated in prose, correctly -- check the current
sentence against the code, not only that it is still there),
"moved-to:<path>" (now elsewhere, correctly), "link" (now a
STATUS.yaml#<id> reference whose claim says the same thing, correctly),
"drop+reason" (genuinely gone -- say whether that is fine because the
code contradicted it, fine because it is an exact duplicate, or a real
loss). A statement that was true, is still needed, and is now nowhere =
real loss: list it in real_losses and set verdict FAIL. Default to FAIL
whenever you are not sure, rather than assuming it is fine; count those
in default_fail_count. A statement the pre-edit doc got wrong and the
current doc now states correctly is "keep-inline" with the correction
named in target_or_reason. Cite file:line for anything you checked;
never restate a secret, hostname or IP value even to quote it.
`

const promptA = `
You are mapping auditor "A". You did not write any of this run's edits.
${COMMON}
Your sample is fixed: read ${SAMPLE_PATH} (JSON list of
{old_location, old_claim_quote, normative_claim_text, code_area_hint};
old_location is <doc>:<line> in the pre-edit file). Audit every entry.
${METHOD}
Write your JSON to ${OUT}/audit-A.json and return it: auditor "A",
sampled [{old_claim_quote, old_location, verdict, target_or_reason}],
default_fail_count, real_losses, verdict.
`

const B_SPREAD = Math.max(1, Math.ceil(DOCS.length * 0.6))

const promptB = `
You are mapping auditor "B". You did not write any of this run's edits.
${COMMON}
Method: pick 15-20 normative statements yourself from the pre-edit text
of these documents (git show HEAD:<doc>): ${DOCS.join(', ')}.
Spread your sample across at least ${B_SPREAD} of the ${DOCS.length}
documents; prefer statements about behavior, wiring, commands, limits,
refusals and statuses over headings and prose filler. Do not reuse
another auditor's sample; choose statements a careful maintainer would
miss if they vanished.
${METHOD}
Write your JSON to ${OUT}/audit-B.json and return it: auditor "B",
sampled [{old_claim_quote, old_location, verdict, target_or_reason}],
default_fail_count, real_losses, verdict.
`

phase('Audit')
log('I.6 mapping audit: auditor A (fixed sample) and auditor B (self-sampled)')
const labels = ['audit:A', 'audit:B']
const raw = await parallel([
  () => agent(promptA, { label: 'audit:A', phase: 'Audit', ...M.audit, schema: AUDIT_SCHEMA }),
  () => agent(promptB, { label: 'audit:B', phase: 'Audit', ...M.audit, schema: AUDIT_SCHEMA }),
])
const audits = raw.filter(Boolean)
const failed = labels
  .map((l, i) => (raw[i] ? null : { label: l, phase: 'Audit', reason: 'agent returned null' }))
  .filter(Boolean)
const ok = failed.length === 0
const counts = { Audit: { ok: audits.length, failed: labels.length - audits.length } }
log(`audits returned ${audits.length}/${labels.length}; ok=${ok}`)

return {
  audits: audits.map(a => ({
    auditor: a.auditor, verdict: a.verdict, default_fail_count: a.default_fail_count,
    real_losses: a.real_losses, sampled: a.sampled.length,
    verdicts: a.sampled.map(s => `${s.old_location} -> ${s.verdict}`),
  })),
  ok,
  counts,
  failed,
}
