// args: {repo, python, date, batches: [batchFilePath, ...], models?}
// Phases: Review (one batch per agent, read-only against the working
// tree) then Judge (refutes every actionable proposal in that batch).
// A batch with nothing actionable skips the judge call entirely.
// Result: {results: [{batch, proposals, verdicts}], missing, ok, counts, failed}
export const meta = {
  name: 'ground-truth-recon-review',
  description: 'Reconcile STATUS.yaml notes and debt descriptions whose cited lines were rewritten or removed by a doc pass',
  phases: [
    { title: 'Review', detail: 'one batch of entries per agent, read-only verification against the working tree' },
    { title: 'Judge', detail: 'refute every actionable proposal' },
  ],
}

const DEFAULTS = {
  prosecutor: { model: 'opus', effort: 'high' },
  reviewer: { model: 'opus' },
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

const SAFETY = `Never copy a secret value, hostname or IP address into your output; cite
file:line instead. READ-ONLY task: do not edit, create, move or delete any
file in the repository and do not run a git command that changes state (no
add/commit/checkout/stash/reset). Allowed: cat, sed -n, rg, git show
HEAD:<path>, git diff HEAD -- <path>, git log.`

const COMMON = `Repository worktree: ${A.repo} (a git worktree; uncommitted working tree).
Python for tools: ${A.python}. Today: ${A.date}.

Context. STATUS.yaml at the repo root is a status contract: claims (entries
with status:/note:) and debt entries (entries with ref:/description:). Notes
and descriptions cite file lines as path:N, path:N-M or "path line N". A doc
reconciliation pass rewrote many documents and comments in this worktree, after
which a line-remapping tool shifted every citation from a base commit's
numbering to the working-tree numbering. Three things the tool could not
settle are your job: (a) "replaced" anchors: the cited line was rewritten, so
the new line may say something different from what the note describes; (b)
"deleted" anchors: the cited line no longer exists; (c) association risk: a
detached citation like "at :190" was assigned to the nearest preceding file
path, which can be the wrong file when a sentence names two files. Also, some
debt entries describe a doc defect that the reconciliation pass has since
fixed, so the debt text may be stale. A debt on a stub, partial or absent
claim stays open: the gap it records is in the code, and the doc edit only
removed a false promise about it. A debt on an implemented claim may no
longer exist.

${IDENTITY}`

const PROPOSAL_SCHEMA = {
  type: 'object',
  properties: {
    proposals: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          id: { type: 'string' },
          action: { type: 'string', enum: ['none', 'rewrite', 'resolve', 'remove'] },
          field: { type: 'string', enum: ['note', 'description'] },
          new_text: { type: 'string', description: 'FULL replacement value of the field for action=rewrite; single line; no ": " sequence; no # character; empty otherwise' },
          evidence: { type: 'string', description: 'file:line facts you verified in the working tree, one sentence per fact' },
          raise_candidate: { type: 'boolean', description: 'true when the evidence says the referenced claim now deserves a higher status; never change status text yourself' },
          raise_reason: { type: 'string' },
          confidence: { type: 'string', enum: ['high', 'medium', 'low'] },
          escalate: { type: 'string', description: 'question for the decider when you cannot settle the entry; empty otherwise' },
        },
        required: ['id', 'action', 'evidence', 'confidence', 'raise_candidate'],
      },
    },
  },
  required: ['proposals'],
}

const VERDICT_SCHEMA = {
  type: 'object',
  properties: {
    verdicts: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          id: { type: 'string' },
          upheld: { type: 'boolean' },
          reason: { type: 'string' },
          corrected_text: { type: 'string', description: 'when a rewrite is right in substance but one anchor or fact is off, the corrected FULL field value (single line, no ": ", no #); empty otherwise' },
        },
        required: ['id', 'upheld', 'reason'],
      },
    },
  },
  required: ['verdicts'],
}

function reviewPrompt(p, idx) {
  return `${SAFETY}\n\n${COMMON}\n\nYour batch: read ${p} (JSON). It holds "items": entries from STATUS.yaml
with fields id, kind (claim|debt), status (claims) or ref + ref_status (debts),
flags, cites (each with path, old and new line numbers and, where available,
old_text from the base commit and new_text from the working tree), hints, and
text (the current entry block verbatim).

For EVERY item, in order:
1. Locate each flagged citation in the working tree (sed -n 'N,Mp' <path>).
   For flags low_sim / substantive: does the new line hold what the note or
   description says it holds? If not, find where that content lives now
   (git show HEAD:<path> | sed -n 'N,Mp' shows what was cited; rg -n finds it
   in the working tree) or establish that it is gone.
   For flag deleted: the line is gone; find the replacement statement, or
   establish that the doc no longer says it.
   For flag prose_far: the citation is a prose "line N" mention rather than a
   path:N anchor; confirm which file it belongs to from the sentence, then
   verify the number points at the described content in that file.
   For flag assoc_risk: for each detached or prose citation, decide which
   file it belongs to from the sentence, then verify the number points at the
   described content in that file; if it was assigned to the wrong file, work
   out the right number from git show HEAD:<file> and the working tree.
   For flag quoted_removed: the note or description repeats words of a line
   the doc pass deleted (the cite carries path and the deleted text); read
   the replacement lines in the working tree and decide whether the quoted
   statement is still what the file says. If not, reword the entry to
   describe the file as it is now; do not re-quote the deleted line.
2. For a debt entry: look at ref_status first. ref_status stub, partial or
   absent: the gap the debt records lives in the code (the thing is still
   missing or a stub), and a doc edit only removed a false promise about it;
   the defect is not gone. Use action "rewrite" with the full corrected
   description: keep every fact about the code that still holds, replace the
   stale doc quote with what the doc says now, cite the new doc lines
   (path:N or path:N-M) and keep the debt open; never "resolve" such an
   entry. ref_status implemented: decide whether the described defect still
   exists in the working tree. Defect gone -> action "resolve" (the apply
   step handles the state field). Defect still there but the lines or
   wording are stale -> action "rewrite" with the full corrected
   description. Defect intact and text accurate -> action "none".
3. For a claim: if the cited facts changed (a doc now says something else, a
   line moved, a count changed), action "rewrite" with the full corrected
   note: keep its structure and every fact that still holds, change only what
   is stale, keep file:line anchors that resolve. Never change the status
   field or the check. If the evidence suggests the claim now deserves a
   higher status (the gap it records has closed), set raise_candidate=true
   and explain in raise_reason, and still leave the text describing the
   current facts. Everything accurate -> action "none".
4. Use action "remove" only for a debt entry that duplicates another debt
   entry on the same ref word for word or in substance (name the duplicate in
   evidence).

Text rules for new_text: one line; never contain the two-character sequence
colon+space; never contain the # character; no citation of CLAUDE.md; plain
ASCII quotes; anchors as path:N or path:N-M; every anchor you write must
resolve in the working tree (you checked it). Do not invent facts you did not
verify.

Report every item in the batch, including action "none" ones, with evidence.
Return only the structured output.`
}

function judgePrompt(p, act, idx) {
  return `${SAFETY}\n\n${COMMON}\n\nYou are the refuter. A reviewer proposed changes to STATUS.yaml entries; the
entries are in ${p} (JSON, field "items", each with id and the current entry
text). The proposals to check:\n${JSON.stringify(act, null, 1)}\n\nFor each proposal try to refute it against the working tree:
- action "resolve": a debt whose ref_status is stub, partial or absent is
  never resolved by a doc edit (its gap is in the code) -> upheld=false, name
  the rule. Otherwise: is the described defect really gone? Read the current
  doc or code the debt points at (sed -n, rg). If any part of the defect remains,
  upheld=false and say what remains.
- action "rewrite": does every path:N anchor in new_text resolve to the
  content it describes (sed -n 'Np' <path>)? Is every fact in new_text true in
  the working tree? Did the reviewer drop a fact from the old text that still
  holds? If the substance is right but one anchor or fact is off, set
  upheld=true and put the corrected FULL field value in corrected_text (one
  line, no colon+space, no #). If the rewrite is wrong in substance,
  upheld=false.
- action "remove": is it a true duplicate? Otherwise upheld=false.
- raise_candidate=true: does the working tree really show the gap closed, and
  would the claim's check (the probe or test named in the entry) need to
  change to prove the new status? Report in reason; upheld refers to the
  whole proposal.
Default to upheld=false when uncertain. Return only the structured output.`
}

const results = await pipeline(
  A.batches,
  (p, item, idx) => agent(reviewPrompt(p, idx), { label: `review:${idx + 1}`, phase: 'Review', ...M.reviewer, schema: PROPOSAL_SCHEMA }),
  (r, p, idx) => {
    if (!r) return null
    const act = r.proposals.filter(x => x.action !== 'none' || x.raise_candidate)
    if (!act.length) return { batch: p, proposals: r.proposals, verdicts: [] }
    return agent(judgePrompt(p, act, idx), { label: `judge:${idx + 1}`, phase: 'Judge', ...M.prosecutor, schema: VERDICT_SCHEMA })
      .then(v => ({ batch: p, proposals: r.proposals, verdicts: v ? v.verdicts : null }))
  },
)
const done = results.filter(Boolean)
const missing = results.length - done.length
const nProp = done.reduce((s, r) => s + r.proposals.filter(x => x.action !== 'none').length, 0)
const nRaise = done.reduce((s, r) => s + r.proposals.filter(x => x.raise_candidate).length, 0)
log(`batches done ${done.length}/${A.batches.length}, actionable proposals ${nProp}, raise candidates ${nRaise}`)
const failed = results
  .map((r, idx) => {
    if (!r) return { label: `review:${idx + 1}`, phase: 'Review', reason: 'no result' }
    if (r.verdicts === null) return { label: `judge:${idx + 1}`, phase: 'Judge', reason: 'no result' }
    return null
  })
  .filter(Boolean)
const judgeFailed = failed.filter(f => f.phase === 'Judge').length
return {
  results: done,
  missing,
  ok: failed.length === 0,
  counts: {
    Review: { ok: done.length, failed: missing },
    Judge: { ok: done.length - judgeFailed, failed: judgeFailed },
  },
  failed,
}
