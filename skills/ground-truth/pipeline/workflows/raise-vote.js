// args: {repo, python, date, claims: [{id, check, docs, code}], models?}
// Phase Refute: three independent refuters per claim, each from a distinct
// angle (the check, the code, the contract), defaulting to refuted=true
// when unsure. A claim survives only if a minority of refuters refuted it.
// Result: {votes: [{id, verdicts: [{refuted, reason, evidence}]}], ok, counts, failed}
export const meta = {
  name: 'ground-truth-raise-vote',
  description: 'Adversarial three-vote check on a claim status raise (a lower status corrected to a higher one)',
  phases: [{ title: 'Refute', detail: 'three independent refuters per claim' }],
}

const DEFAULTS = {
  prosecutor: { model: 'opus', effort: 'high' },
}
const M = Object.assign({}, DEFAULTS, args.models || {})

const A = args

const IDENTITY = `Your output may end up committed to a pushed repository. Write plain,
neutral engineering prose, as any developer on the team would. Never
mention Claude, Anthropic, ChatGPT, GPT, Copilot, LLM, "AI",
"AI-assisted", "AI-generated", "coding agent", "language model",
"assistant", or add a Co-Authored-By line anywhere -- not in claim
text, notes, code comments, commit messages, or filenames. Avoid:
delve, leverage, comprehensive, robust, seamless, streamline,
consolidate, modernize, enhanced, utilize, facilitate.`

const COMMON = `Repository worktree: ${A.repo}. Read-only task: do not edit any file, never
run a git command that changes state (no add/commit/rm/mv/checkout/stash/reset).
Never print, copy or paraphrase a secret value, hostname or IP address; refer
to such things by file:line only. Python for this repo: ${A.python}. Today:
${A.date}. This is a company repository: do not send any of its content to
any external API or web service.

The status contract is ${A.repo}/STATUS.yaml; grep it, never read it whole:
rg -n -A14 '^- id: <claim_id>$' STATUS.yaml

${IDENTITY}`

const VERDICT = {
  type: 'object',
  properties: {
    refuted: { type: 'boolean' },
    reason: { type: 'string' },
    evidence: { type: 'array', items: { type: 'string' } },
  },
  required: ['refuted', 'reason', 'evidence'],
}

const ANGLES = [
  'the check -- is there a way the document could regress to the old defect, or drift in a similar way, without this probe going red? Run the probe, read its code, try one concrete mutation of the document in your head and say whether the probe catches it',
  'the code -- does every sentence of the document that the claim covers actually hold in the code today? Read the code, not the note; name any sentence that is stronger or weaker than what the code does',
  'the contract -- read the claim note and its debt entries in STATUS.yaml; does the note describe the current state truthfully, do line anchors in the note point at the lines they name, is any debt still open that should keep the claim at its prior status, and does any other claim on the same path contradict it',
]

function refutePrompt(c, k) {
  return `${COMMON}\n\nYou are refuter ${k + 1} of ${ANGLES.length} for one claim. Your job is to
REFUTE the following proposition, and to default to refuted=true when you are
not sure.

Proposition: claim \`${c.id}\` in STATUS.yaml is correctly marked at its current
(raised) status. The status was raised on ${A.date}; the reason it was lower
before is described in the claim's note, the defect was fixed, and the check
was changed to pin the corrected state.

Check file: ${c.check}
Document(s): ${c.docs.join(', ')}
Code the claim is about: ${c.code.join(', ')}

Angle for refuter ${k + 1}: ${ANGLES[k]}

Return refuted=true with a concrete reason and file:line evidence if you find
anything that makes the raised status unwarranted. Return refuted=false only
if you looked and found nothing; say what you checked in evidence.`
}

// Every refuter task resolves to a well-formed {claim, refuter, ok, verdict}
// -- never to null and never rejecting -- so a claim can never lose a vote
// silently. ok=true means the agent actually answered; ok=false means it
// returned null or the call itself failed, and verdict was defaulted to
// refuted=true (fail-closed): a raise that could not be checked does not
// pass the gate.
const flat = await parallel(
  A.claims.flatMap(c => ANGLES.map((_, k) => () =>
    agent(refutePrompt(c, k), { label: `refute:${c.id}:${k + 1}`, phase: 'Refute', ...M.prosecutor, schema: VERDICT })
      .then(
        v => ({ claim: c.id, refuter: k + 1, ok: !!v, verdict: v || { refuted: true, reason: 'agent returned null', evidence: [] } }),
        () => ({ claim: c.id, refuter: k + 1, ok: false, verdict: { refuted: true, reason: 'agent call failed', evidence: [] } }),
      ),
  )),
)

const byClaim = {}
for (const r of flat) {
  if (!r) continue
  ;(byClaim[r.claim] = byClaim[r.claim] || []).push(r)
}
const votes = A.claims.map(c => ({
  id: c.id,
  verdicts: (byClaim[c.id] || [])
    .map(r => ({ refuted: r.verdict.refuted, reason: r.verdict.reason, evidence: r.verdict.evidence })),
}))
const failed = []
for (const c of A.claims) {
  const got = byClaim[c.id] || []
  for (const r of got) {
    if (!r.ok) failed.push({ label: `refute:${c.id}:${r.refuter}`, phase: 'Refute', reason: 'agent returned null, voted refuted (fail-closed)' })
  }
  for (let k = got.length; k < ANGLES.length; k++) {
    failed.push({ label: `refute:${c.id}:${k + 1}`, phase: 'Refute', reason: 'no result' })
  }
}
const okCount = flat.filter(r => r && r.ok).length
log(`claims voted ${votes.length}, refuter calls ok ${okCount}/${A.claims.length * ANGLES.length} (any missing call votes refuted, fail-closed)`)

return {
  votes,
  ok: failed.length === 0,
  counts: { Refute: { ok: okCount, failed: failed.length } },
  failed,
}
