// args: {repo, python, guard, docs: [{doc, slug, brief_file, n_conflicts}],
//        areas: [{area, slug, brief_file}], models?}
// Roles: docs classifier/editor/reviewer/fixer and area comment
// editor/reviewer/fixer all use M.docfix for the editing steps and
// M.reviewer for the review steps (see DEFAULTS below).
// Result: {docs, comments, overrides, proposals, ok, counts, failed}
//   docs[]/comments[] follow reviewLoop's shape (edit, review, rounds,
//   history, override_needed); overrides lists "doc:<path>" / "area:<name>"
//   entries whose review never reached PASS within MAX_FAIL_ROUNDS;
//   proposals lists {doc, verdict, reason_one_line, duplicate_of_path}
//   for every document the classifier marked move_to_docs_human or
//   delete_candidate -- nothing is moved or deleted here, a human applies
//   an approved proposal with git mv / git rm.
export const meta = {
  name: 'ground-truth-i5',
  description: 'Doc reconciliation: classify/edit/review markdown docs, comment-only reconcile over coverage areas',
  phases: [
    { title: 'Docs', detail: 'per document: classifier, editor, reviewer, fixer loop (max 2 FAIL rounds)' },
    { title: 'Comments', detail: 'per coverage area: comment/docstring editor, guard, reviewer, fixer loop' },
  ],
}

const DEFAULTS = {
  docfix: { model: 'opus' },
  reviewer: { model: 'opus' },
}
const M = Object.assign({}, DEFAULTS, args.models || {})

const IDENTITY = `Your output may end up committed to a pushed repository. Write plain,
neutral engineering prose, as any developer on the team would. Never
mention Claude, Anthropic, ChatGPT, GPT, Copilot, LLM, "AI",
"AI-assisted", "AI-generated", "coding agent", "language model",
"assistant", or add a Co-Authored-By line anywhere -- not in claim
text, notes, code comments, commit messages, or filenames. Avoid:
delve, leverage, comprehensive, robust, seamless, streamline,
consolidate, modernize, enhanced, utilize, facilitate.`

const SENSITIVE = `Never print, copy or paraphrase a secret value, hostname or IP
address anywhere in your output or in a file you edit; refer to such
things by file:line instead. Do not send repository content to any
external API or web service.`

const a = args
const COMMON = `Repository worktree: ${a.repo} (a git worktree; other people work in the
main tree, never touch anything outside this worktree). Other agents edit
other files in the same worktree at the same time, so: never run git
commands that change state (no add, commit, stash, checkout, restore,
clean, reset, mv, rm); read-only git (status, diff, show, grep, ls-files)
is fine. Never edit a file outside your scope block. No network access.
The contract file is ${a.repo}/STATUS.yaml; a claim id is valid only if
"grep -n 'id: <id>$' STATUS.yaml" finds exactly one line. Code is the
source of truth: a conflict between a document and the code is resolved
by changing the document, never the code; a stub is described as a stub.
Set forbidden_action_taken=true only if you did something these rules
forbid; never do it and report false.

${SENSITIVE}
${IDENTITY}`

const DOC_SCOPE = (d) => `SCOPE (identical for editor, reviewer and fixer of this document):
- editable: exactly one file, ${d.doc}. Nothing else.
- brief: ${d.brief_file} (JSON; read it first). Fields: conflicts
  [{claim_id, conflict}] = statements in this document that the review
  found wrong or stale, each with the reason; final_claims [{claim_id,
  kind, status, check, note}] = the settled truth for every claim this
  document touches; i1_claims = the sentences originally extracted from
  this document (with line numbers as of the unedited file); debt_mentions
  = debt entries whose text points at this document.
- required outcome: every entry in conflicts is either rewritten in the
  document so the sentence matches final_claims (and, where a status is
  restated, replaced by or followed by a reference of the form
  STATUS.yaml#<claim_id>), or reported as not_a_conflict with a reason
  that a reviewer can check against the code.
- forbidden in the document: notes about what changed or why, changelog
  lines, "as of <date>" markers, banned words from the identity rule,
  references to claim ids that do not exist in STATUS.yaml, new hostnames
  / IPs / credentials, edits to sentences that no conflict entry names
  (leave unrelated prose exactly as it is, including its style).
- line numbers in the brief refer to the unedited file; after the first
  edit locate text by the quoted words, not by line.`

const AREA_SCOPE = (c) => `SCOPE (identical for editor, reviewer and fixer of this area):
- brief: ${c.brief_file} (JSON; read it first). Shape: {area, files:
  {<path>: [{claim_id, conflict}]}}. Each conflict names a comment or
  docstring in that file whose wording disagrees with what the code does.
- editable: only the files listed in the brief, and inside them only
  comment text: "#" comment lines and trailing "# ..." comments, Python
  docstrings (the string statement that opens a module/class/function),
  YAML "#" comments, Jinja "{# ... #}" blocks. Never a code token, a YAML
  key or value, a string literal that is not a docstring, whitespace that
  is not inside a comment, a shebang line, or the order of anything.
- test files (paths containing /tests/ or a basename starting with test_)
  are never edited.
- required outcome: every conflict in the brief is either fixed by
  rewording the comment/docstring so it matches the code, or listed under
  skipped with reason "code_behaviour" (the disagreement is in the code,
  not in the comment, so a comment edit cannot resolve it) or
  "not_a_conflict" (with a reason a reviewer can check). A dangling path
  or identifier that resolves nowhere is fixed by removing the reference,
  not by inventing a new one.
- structure guard: after editing run "${a.guard} --file <path> [--file <path> ...]"
  with every file you touched. It prints OK per file when only
  comments/docstrings changed and CHANGED-STRUCTURE otherwise. Every file
  must print OK before you finish; if one does not, re-read
  "git diff -- <path>" and repair by hand (no git checkout/restore). Files
  ending in .j2 are compared with comment lines stripped; edit only "{# #}"
  or "#" comment lines there.
- forbidden in comments: notes about what changed, dates, banned words,
  hostnames / IPs / credentials.`

const CLASS_SCHEMA = { type: 'object', properties: {
  doc_path: { type: 'string' },
  verdict: { type: 'string', enum: ['keep', 'move_to_docs_human', 'delete_candidate'] },
  reason_one_line: { type: 'string' },
  duplicate_of_path: { type: ['string', 'null'] },
}, required: ['doc_path', 'verdict', 'reason_one_line', 'duplicate_of_path'] }

const EDIT_SCHEMA = { type: 'object', properties: {
  doc_path: { type: 'string' },
  changes: { type: 'array', items: { type: 'object', properties: { old_text: { type: 'string' }, new_text: { type: 'string' }, claim_ids: { type: 'array', items: { type: 'string' } } }, required: ['old_text', 'new_text', 'claim_ids'] } },
  claims_referenced: { type: 'array', items: { type: 'string' } },
  not_a_conflict: { type: 'array', items: { type: 'object', properties: { claim_id: { type: 'string' }, conflict_excerpt: { type: 'string' }, why: { type: 'string' } }, required: ['claim_id', 'conflict_excerpt', 'why'] } },
  out_of_scope: { type: 'array', items: { type: 'string' } },
  citing_entries_touched: { type: 'array', items: { type: 'object', properties: { id: { type: 'string' }, old_anchor: { type: 'string' }, new_anchor: { type: 'string', description: 'the citation into this document as it now reads, or the literal string "stale" if the cited text is gone' } }, required: ['id', 'old_anchor', 'new_anchor'] } },
  forbidden_action_taken: { type: 'boolean' },
}, required: ['doc_path', 'changes', 'claims_referenced', 'not_a_conflict', 'out_of_scope', 'citing_entries_touched', 'forbidden_action_taken'] }

const REVIEW_SCHEMA = { type: 'object', properties: {
  doc_path: { type: 'string' },
  verdict: { type: 'string', enum: ['PASS', 'FAIL'] },
  items: { type: 'array', items: { type: 'object', properties: { ref: { type: 'string' }, status: { type: 'string', enum: ['ok', 'missing', 'wrong', 'forbidden_edit'] }, detail: { type: 'string' } }, required: ['ref', 'status', 'detail'] } },
  other_files_touched: { type: 'array', items: { type: 'string' } },
  banned_words_found: { type: 'array', items: { type: 'string' } },
  bad_claim_ids: { type: 'array', items: { type: 'string' } },
  summary: { type: 'string' },
}, required: ['doc_path', 'verdict', 'items', 'other_files_touched', 'banned_words_found', 'bad_claim_ids', 'summary'] }

const CEDIT_SCHEMA = { type: 'object', properties: {
  area: { type: 'string' },
  files: { type: 'array', items: { type: 'object', properties: {
    path: { type: 'string' }, edited: { type: 'boolean' },
    addressed: { type: 'array', items: { type: 'string' } },
    skipped: { type: 'array', items: { type: 'object', properties: { claim_id: { type: 'string' }, reason: { type: 'string', enum: ['code_behaviour', 'not_a_conflict', 'test_file', 'other'] }, detail: { type: 'string' } }, required: ['claim_id', 'reason', 'detail'] } },
  }, required: ['path', 'edited', 'addressed', 'skipped'] } },
  guard_ok: { type: 'boolean' },
  guard_output: { type: 'string' },
  forbidden_action_taken: { type: 'boolean' },
}, required: ['area', 'files', 'guard_ok', 'guard_output', 'forbidden_action_taken'] }

const CREVIEW_SCHEMA = { type: 'object', properties: {
  area: { type: 'string' },
  verdict: { type: 'string', enum: ['PASS', 'FAIL'] },
  guard_ok: { type: 'boolean' },
  guard_output: { type: 'string' },
  items: { type: 'array', items: { type: 'object', properties: { path: { type: 'string' }, claim_id: { type: 'string' }, status: { type: 'string', enum: ['ok', 'missing', 'wrong', 'forbidden_edit', 'skip_accepted', 'skip_rejected'] }, detail: { type: 'string' } }, required: ['path', 'claim_id', 'status', 'detail'] } },
  other_files_touched: { type: 'array', items: { type: 'string' } },
  banned_words_found: { type: 'array', items: { type: 'string' } },
  summary: { type: 'string' },
}, required: ['area', 'verdict', 'guard_ok', 'guard_output', 'items', 'other_files_touched', 'banned_words_found', 'summary'] }

const classifierPrompt = (d) => `Classify exactly one document: ${a.repo}/${d.doc}. Read the whole
file. Choose exactly one verdict: "keep" (it carries checkable claims about
code and is reconciled by another agent), "move_to_docs_human" (human
context or narrative, nothing actionable, safe to move out of the
cold-start path into docs/_human/), or "delete_candidate" (an exact or
near-exact duplicate of a fact recorded elsewhere -- name the other
location in duplicate_of_path, else null). One line of reason. You are
proposing, not deciding: nothing is moved or deleted from this output; an
approved move or delete is later done with git mv / git rm by the owner.
Do not propose a banned-words exclude for docs/_human/*. Do not edit
anything.

${IDENTITY}`

const editorPrompt = (d) => `Edit exactly one file: ${a.repo}/${d.doc}, so it stops disagreeing with
the code. ${COMMON}

${DOC_SCOPE(d)}

Method: read the brief, then the document, then for each conflict entry
find the sentence(s) it names, read the code the conflict and the
final_claims note point at (enough to word the correction precisely), and
rewrite the sentence directly in place -- do not append "actually ..."
next to the old sentence. Where the document restates a component's
status, replace the restated status with a reference "STATUS.yaml#<id>"
(or keep a short factual phrase followed by that reference) after
confirming the id exists with grep. Where the document describes
something the contract marks stub/partial/absent, say so in the
document's own voice, plainly. If two conflict entries name the same
sentence, resolve both with one rewrite. When you finish, run
"git diff --stat" and confirm only ${d.doc} changed. Report each change
as {old_text, new_text, claim_ids} with exact text.

Also report citing_entries_touched: for every id in final_claims and
debt_mentions whose citation into this document (a quoted sentence,
file:line, or line-number reference) you changed the wording or location
of, give {id, old_anchor, new_anchor} -- old_anchor is the citation text
as it read before your edit, new_anchor is that citation as it now reads,
or the literal string "stale" if the material it cited is gone. Leave the
array empty if no citation into this document moved or changed.`

const reviewerPrompt = (d, round, prev) => `Review the edit of ${a.repo}/${d.doc} (round ${round}). ${COMMON}

${DOC_SCOPE(d)}

Read the brief, then "git diff -- ${d.doc}", then the edited file. For
every conflict entry in the brief produce one item (ref = claim_id plus
the first words of the conflict): ok = the sentence now matches
final_claims and the code; missing = the sentence is unchanged and still
wrong; wrong = it was changed but is still inaccurate or now overclaims
(read the code to decide, do not trust the editor's report); the editor's
not_a_conflict claims count as ok only if you verify the reason against
the code. Add forbidden_edit items for: sentences changed that no conflict
names, notes about the change, banned words, dates, hostnames/IPs, or a
STATUS.yaml#<id> whose id grep does not find. Run "git status --porcelain"
and list any other modified file the diff shows for this document's editor
(files of other documents or code areas are edited by other agents at the
same time -- ignore them, report only if ${d.doc}'s edit spilled into
another file, which you can only infer from the content). Verdict PASS
only when there is no missing/wrong/forbidden_edit item; otherwise FAIL.
${prev ? `Previous round FAIL items were: ${JSON.stringify(prev)}. Re-check exactly those and anything the fix touched.` : ''}
The editor's report: ${JSON.stringify(d._edit).slice(0, 6000)}`

const fixerPrompt = (d, review) => `Fix the remaining problems in ${a.repo}/${d.doc}. ${COMMON}

${DOC_SCOPE(d)}

A reviewer marked these items after the previous edit:
${JSON.stringify(review.items.filter(i => i.status !== 'ok'))}
Other reviewer notes: banned_words_found=${JSON.stringify(review.banned_words_found)},
bad_claim_ids=${JSON.stringify(review.bad_claim_ids)}, summary: ${review.summary}
Address exactly these items in the document (read the code before
rewording; if you believe an item is not a conflict, put it in
not_a_conflict with a checkable reason instead of editing). Touch nothing
else. Report each change as {old_text, new_text, claim_ids}, and
citing_entries_touched as {id, old_anchor, new_anchor} for any citing
claim or debt entry whose anchor into this document your fix moved or
invalidated (empty array if none).`

const ceditorPrompt = (c) => `Reconcile comments and docstrings in the "${c.area}" area of ${a.repo}.
${COMMON}

${AREA_SCOPE(c)}

Method: read the brief; for each file, open it, read the code around each
named comment, and reword the comment/docstring so it states what the code
does now. Keep the file's comment style. After all files: run the guard on
every touched file, paste its output in guard_output, set guard_ok only if
every line says OK. Run "git diff --stat" and confirm only your files
changed.`

const creviewerPrompt = (c, round, prev) => `Review the comment-only edits in the "${c.area}" area of ${a.repo}
(round ${round}). ${COMMON}

${AREA_SCOPE(c)}

Read the brief, run "git diff -- <every file in the brief>", and run the
guard yourself on every file the diff shows as modified in this area; paste
its output in guard_output; guard_ok = every line OK. For every conflict in
the brief produce one item: ok (comment now matches the code -- read the
code, do not trust the editor), missing (unchanged and still wrong), wrong
(changed but still inaccurate), skip_accepted / skip_rejected for entries
the editor listed under skipped (accept "code_behaviour" only if the
disagreement really is in the code, "not_a_conflict" only if the reason
holds). forbidden_edit for any non-comment change, any change in a file
outside the brief, banned words, dates, hostnames/IPs, or the test-file
rule broken. Verdict PASS only with guard_ok and no missing/wrong/
forbidden_edit/skip_rejected item; otherwise FAIL.
${prev ? `Previous round FAIL items: ${JSON.stringify(prev)}. Re-check exactly those and anything the fix touched.` : ''}
Editor's report: ${JSON.stringify(c._edit).slice(0, 6000)}`

const cfixerPrompt = (c, review) => `Fix the remaining problems in the "${c.area}" area of ${a.repo}.
${COMMON}

${AREA_SCOPE(c)}

A reviewer marked these items after the previous edit:
${JSON.stringify(review.items.filter(i => !['ok', 'skip_accepted'].includes(i.status)))}
guard_ok=${review.guard_ok}; guard_output: ${review.guard_output.slice(0, 1500)}
banned_words_found=${JSON.stringify(review.banned_words_found)}; summary: ${review.summary}
Address exactly these items (comment text only; if the guard said
CHANGED-STRUCTURE, restore the structure by hand first). Run the guard on
every touched file before finishing; guard_ok = every line OK.`

const MAX_FAIL_ROUNDS = 2

async function reviewLoop(item, doEdit, doReview, doFix) {
  const edit = await doEdit()
  if (!edit) return { edit: null, review: null, rounds: 0, override_needed: true, failed: 'editor' }
  item._edit = edit
  let review = await doReview(1, null)
  let rounds = 0
  const history = [{ edit, review }]
  while (review && review.verdict === 'FAIL' && rounds < MAX_FAIL_ROUNDS) {
    rounds++
    const fix = await doFix(review)
    if (!fix) break
    item._edit = fix
    review = await doReview(rounds + 1, review.items.filter(i => !['ok', 'skip_accepted'].includes(i.status)))
    history.push({ fix, review })
  }
  return { edit, review, rounds, history, override_needed: !review || review.verdict === 'FAIL' }
}

if (a.docs.length > 60) {
  log(`${a.docs.length} documents exceeds 60 -- consider running the review phase as a second workflow (D39)`)
}

const docsRun = pipeline(a.docs,
  d => agent(classifierPrompt(d), { label: `classify:${d.slug}`, phase: 'Docs', ...M.docfix, schema: CLASS_SCHEMA }),
  async (cls, d) => {
    if (d.n_conflicts === 0) {
      return { doc: d.doc, slug: d.slug, classification: cls, edit: null, review: null, rounds: 0, override_needed: false, skipped: 'no_conflicts' }
    }
    const r = await reviewLoop(d,
      () => agent(editorPrompt(d), { label: `edit:${d.slug}`, phase: 'Docs', ...M.docfix, schema: EDIT_SCHEMA }),
      (round, prev) => agent(reviewerPrompt(d, round, prev), { label: `review${round}:${d.slug}`, phase: 'Docs', ...M.reviewer, schema: REVIEW_SCHEMA }),
      (review) => agent(fixerPrompt(d, review), { label: `fix:${d.slug}`, phase: 'Docs', ...M.docfix, schema: EDIT_SCHEMA }),
    )
    return { doc: d.doc, slug: d.slug, classification: cls, ...r }
  },
)

const commentsRun = pipeline(a.areas,
  async (c) => {
    const r = await reviewLoop(c,
      () => agent(ceditorPrompt(c), { label: `cedit:${c.slug}`, phase: 'Comments', ...M.docfix, schema: CEDIT_SCHEMA }),
      (round, prev) => agent(creviewerPrompt(c, round, prev), { label: `creview${round}:${c.slug}`, phase: 'Comments', ...M.reviewer, schema: CREVIEW_SCHEMA }),
      (review) => agent(cfixerPrompt(c, review), { label: `cfix:${c.slug}`, phase: 'Comments', ...M.docfix, schema: CEDIT_SCHEMA }),
    )
    return { area: c.area, slug: c.slug, ...r }
  },
)

const [docs, comments] = await parallel([() => docsRun, () => commentsRun])
const docsOk = docs.filter(Boolean)
const commentsOk = comments.filter(Boolean)
const overrides = [
  ...docsOk.filter(x => x.override_needed).map(x => `doc:${x.doc}`),
  ...commentsOk.filter(x => x.override_needed).map(x => `area:${x.area}`),
]
const proposals = docsOk.filter(x => x.classification && x.classification.verdict !== 'keep')
  .map(x => ({ doc: x.doc, ...x.classification }))
const failed = [
  ...docs.map((r, i) => r ? null : { label: `doc:${a.docs[i].slug}`, phase: 'Docs', reason: 'no result' }).filter(Boolean),
  ...comments.map((r, i) => r ? null : { label: `area:${a.areas[i].slug}`, phase: 'Comments', reason: 'no result' }).filter(Boolean),
]
log(`docs ${docsOk.length}/${a.docs.length}, areas ${commentsOk.length}/${a.areas.length}, override needed: ${overrides.length}, move/delete proposals: ${proposals.length}`)
return {
  docs, comments, overrides, proposals,
  ok: failed.length === 0,
  counts: { Docs: { ok: docsOk.length, failed: a.docs.length - docsOk.length }, Comments: { ok: commentsOk.length, failed: a.areas.length - commentsOk.length } },
  failed,
}
