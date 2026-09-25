// I.1 research sweep: read-only finders that turn a repository into the
// raw material for a status contract.
//
// args:
//   repo      - absolute path to the repository (or worktree) root
//   research  - absolute path to the directory the finders write into
//               (usually run.research)
//   roots     - [{name, path, language}], one entry per coverage root;
//               path is repo-relative. One codemap finder runs per entry.
//   models    - optional {role: {model, effort?}} override map
//
// Roles: inventory (M.inventory), claims/codemap/debt (M.finder). Four
// finder kinds, len(roots) + 3 agents total, all read-only and run in
// parallel.
//
// Result: { inventory, claims, codemap, debt, ok, counts, failed }.
//   inventory - {files, coldstart_before, chain, written_to} | null
//   claims    - {count, by_source, written_to} | null
//   codemap   - { <root.name>: {components, orphans, written_to} | null }
//   debt      - {suspects, by_kind, written_to} | null
//   ok        - true when every finder returned a result
//   counts    - { Research: { ok, failed } }
//   failed    - [{label, phase, reason}]
//
// Each finder is asked to write its own JSON file into args.research, but
// the file it writes is not what the schema validated -- its structured
// return value is. Run i1_check_research with --journal pointing at this
// workflow's journal.jsonl: the stage rewrites the four files from the
// journal and only then checks them against pipeline/schemas/*.schema.json.

export const meta = {
  name: 'ground-truth-i1-research',
  description: 'Phase I.1 research sweep: inventory, claims, codemap (one per root), debt suspects',
  phases: [{ title: 'Research', detail: 'read-only finders in parallel' }],
}

const DEFAULTS = {
  inventory: { model: 'opus' },
  finder: { model: 'opus' },
}
const M = Object.assign({}, DEFAULTS, args.models || {})

const REPO = args.repo
const OUT = args.research
const ROOTS = args.roots
if (!Array.isArray(ROOTS) || ROOTS.length === 0 ||
    ROOTS.some(r => !r || typeof r !== 'object' || typeof r.name !== 'string' || typeof r.path !== 'string' || typeof r.language !== 'string')) {
  throw new Error('args.roots must be a non-empty array of {name, path, language} objects (path repo-relative); got ' + JSON.stringify(ROOTS))
}

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
Work only inside this path. Use rg/grep for searching rather than a
full directory walk. Skip docs/_human/ entirely -- it holds quarantined
human-authored narrative, not material for this sweep.
${SENSITIVE}
${IDENTITY}
`

const INVENTORY_SCHEMA = {
  type: 'object',
  properties: {
    files: { type: 'array', items: { type: 'object', properties: {
      path: { type: 'string' }, bytes: { type: 'integer' }, purpose_one_line: { type: 'string' } },
      required: ['path', 'bytes', 'purpose_one_line'] } },
    onboarding_chain: { type: 'array', items: { type: 'string' } },
    coldstart_bundle_before_bytes: { type: 'integer' },
    written_to: { type: 'string' },
  },
  required: ['files', 'onboarding_chain', 'coldstart_bundle_before_bytes', 'written_to'],
}

const CLAIMS_SCHEMA = {
  type: 'object',
  properties: {
    claims_found: { type: 'array', items: { type: 'object', properties: {
      quote: { type: 'string' }, file: { type: 'string' }, line: { type: 'integer' },
      claim: { type: 'string' }, hint: { type: 'string' },
      source: { type: 'string', enum: ['head-doc', 'prior-status-yaml', 'pending-diff', 'code-comment', 'ci-config'] } },
      required: ['quote', 'file', 'line', 'claim', 'hint', 'source'] } },
    written_to: { type: 'string' },
  },
  required: ['claims_found', 'written_to'],
}

const CODEMAP_SCHEMA = {
  type: 'object',
  properties: {
    components: { type: 'array', items: { type: 'object', properties: {
      component: { type: 'string' }, root: { type: 'string' }, entry_point: { type: 'string' },
      language: { type: 'string' },
      existing_tests: { type: 'array', items: { type: 'string' } },
      importers: { type: 'array', items: { type: 'string' } },
      note: { type: 'string' } },
      required: ['component', 'root', 'entry_point', 'language', 'existing_tests', 'importers'] } },
    orphan_files: { type: 'array', items: { type: 'string' } },
    written_to: { type: 'string' },
  },
  required: ['components', 'orphan_files', 'written_to'],
}

const DEBT_SCHEMA = {
  type: 'object',
  properties: {
    suspects: { type: 'array', items: { type: 'object', properties: {
      path: { type: 'string' }, line: { type: 'integer' }, kind: { type: 'string' },
      description: { type: 'string' }, severity: { type: 'string', enum: ['low', 'medium', 'high'] } },
      required: ['path', 'line', 'kind', 'description', 'severity'] } },
    written_to: { type: 'string' },
  },
  required: ['suspects', 'written_to'],
}

function slugify(name) {
  const s = String(name).toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '')
  return s || 'root'
}

const seenSlugs = {}
const SLUGS = ROOTS.map(r => {
  const base = slugify(r.name)
  seenSlugs[base] = (seenSlugs[base] || 0) + 1
  return seenSlugs[base] > 1 ? `${base}-${seenSlugs[base]}` : base
})

const inventoryPrompt = `
You are cataloguing every knowledge file in the repository -- every
README, doc under docs/, plan, CI config, role README, settings or
example config that a human or a new session reads to understand the
repo (not code, not templates, not inventory data). For each: path,
size in bytes (use wc -c), and its purpose in one line (what a reader
goes there for, not a summary of contents). Do not enter code files
beyond what is needed to confirm a file is documentation. List, in
order, exactly which files the repo's own onboarding text (root
README.md, or a top-level contributor/agent rules file if one already
exists, and
whatever either unconditionally points to) requires a new session to
read (onboarding_chain), and sum their bytes as
coldstart_bundle_before_bytes.
${COMMON}
Write the JSON object you return to ${OUT}/inventory.json (create the
file; overwrite if present) and set written_to to that path.
`

const claimsPrompt = `
Read every documentation file in the repository (README files, docs
under docs/, architecture notes, module or role READMEs) plus any CI
config files (their comments and job definitions state what is tested
and deployed) and the module or package docstrings at the top of the
repository's source files. Extract every normative claim about code or
pipeline behavior -- "X is implemented", "X always happens", "X is
validated", "X is deployed by job Y", "X is read-only" -- with an exact
quote and file:line. For each, guess which code area it is about
(directory or file name) as hint, but do not verify the claim yourself
-- that is a later phase's job. Do not paraphrase away hedged language
("should", "probably", "planned", "TODO") -- preserve it, it is
evidence about the claim's own confidence. Keep the quote verbatim in
whatever language the doc uses; write claim in English. Tag each claim
via source: "head-doc" for a claim from a doc at HEAD, "code-comment"
for a docstring or comment, "ci-config" for a CI file.
${COMMON}
Write the JSON object you return to ${OUT}/claims.json (create the
file; overwrite if present) and set written_to to that path.
`

function codemapPrompt(root) {
  return `
Map every component under this coverage root:
  name: ${root.name}
  path: ${root.path} (relative to the repository root)
  language: ${root.language}

A component is a unit with its own responsibility at the grain that
fits this root: if the root holds source files or modules, a component
is usually one file or one package's public entry file; if the root
holds declarative or deployable units (a deploy role, a playbook, a CI
job, a policy or fixture file consumed by code), a component is usually
one such unit, with its defining file or manifest as entry_point. If
this root is a CI/pipeline config file, treat each named job as a
component with that file as entry_point and the CI system's name as
language.

For each component: a short component name, root (this root's path),
its entry point file (a real path, verified to exist), language, any
existing test file that already exercises it (grep the tests for the
import or the unit's name -- list only tests that really reference it),
and the files that import or invoke it directly (grep/AST, your choice
-- direct references only, no transitive closure). Also list
orphan_files: files under this root that nothing imports, no test
references, and nothing invokes. This is raw material for a status
contract and for cross-component edges written later; do not draft
claim text or a check id yourself. Note in "note" anything odd you see
in passing (a test module that imports nothing from the code it is
named after, a script with two copies of a function, an entry point
that is declared somewhere but does not exist).
${COMMON}
Write the JSON object you return to ${OUT}/codemap-${SLUGS[ROOTS.indexOf(root)]}.json
(create the file; overwrite if present) and set written_to to that path.
`
}

const debtPrompt = `
Scan the repository for stub/debt signals: TODO, FIXME, XXX, HACK,
NotImplementedError, pass-only function bodies, disabled tests (@skip,
skipif, .skip(, xfail without a tracked reason, commented-out test
functions), mocked-in-production paths, always-true/always-false
branches that look load-bearing, empty except/catch blocks, hard-coded
"temporary" values, and CI jobs whose script runs only a subset of the
test files actually present in the repository. Cover every coverage
root: ${ROOTS.map(r => `${r.name} (${r.path}, ${r.language})`).join(', ')},
plus any CI config. For each hit: path, line, a short kind label (e.g.
"todo", "not-implemented", "disabled-test", "mocked-in-prod",
"secret-literal"), a description (one line of quoted context plus what
this looks wired into, if visible nearby -- for a secret-literal hit,
describe the kind of literal instead of quoting it), and a severity of
"low", "medium" or "high" by how load-bearing the surrounding code
looks. You are listing suspects, not verdicts -- do not write "this is
a stub", just show the evidence. Flag as a suspect any literal in
tracked code or config that looks like a key or a credential -- a
private key block, a long random token, a password assigned to a
constant, an unencrypted secret in a vars or env file -- regardless of
whether the path looks live or dead, and regardless of what the docs
say about that file; a secret-literal hit is always at least "high"
severity. Do not copy the value: report path, line, and the kind of
literal it is (kind "secret-literal"). This is a whole-tree scan: skip
STATUS.yaml, tools/ground_truth/, .claude/, and docs/_human/ -- these
hold the contract's own machinery and quarantined narrative, not code
to audit.
${COMMON}
Write the JSON object you return to ${OUT}/debt-candidates.json (create
the file; overwrite if present) and set written_to to that path.
`

phase('Research')
log(`I.1 research sweep started over ${ROOTS.length} root(s)`)
const results = await parallel([
  () => agent(inventoryPrompt, { label: 'finder:inventory', phase: 'Research', schema: INVENTORY_SCHEMA, ...M.inventory }),
  () => agent(claimsPrompt, { label: 'finder:claims', phase: 'Research', schema: CLAIMS_SCHEMA, ...M.finder }),
  ...ROOTS.map((r, i) => () => agent(codemapPrompt(r), { label: `finder:codemap-${SLUGS[i]}`, phase: 'Research', schema: CODEMAP_SCHEMA, ...M.finder })),
  () => agent(debtPrompt, { label: 'finder:debt', phase: 'Research', schema: DEBT_SCHEMA, ...M.finder }),
])

const inv = results[0]
const claims = results[1]
const codemapResults = results.slice(2, 2 + ROOTS.length)
const debt = results[2 + ROOTS.length]

const labels = ['finder:inventory', 'finder:claims', ...ROOTS.map((r, i) => `finder:codemap-${SLUGS[i]}`), 'finder:debt']
const failed = results.map((r, i) => (r ? null : { label: labels[i], phase: 'Research', reason: 'agent returned null' })).filter(Boolean)
const okCount = results.filter(Boolean).length

const codemap = {}
ROOTS.forEach((r, i) => {
  const cm = codemapResults[i]
  codemap[r.name] = cm ? { components: cm.components.length, orphans: cm.orphan_files, written_to: cm.written_to } : null
})

const summary = {
  inventory: inv ? { files: inv.files.length, coldstart_before: inv.coldstart_bundle_before_bytes, chain: inv.onboarding_chain, written_to: inv.written_to } : null,
  claims: claims ? { count: claims.claims_found.length, by_source: claims.claims_found.reduce((m, c) => { m[c.source] = (m[c.source] || 0) + 1; return m }, {}), written_to: claims.written_to } : null,
  codemap,
  debt: debt ? { suspects: debt.suspects.length, by_kind: debt.suspects.reduce((m, s) => { m[s.kind] = (m[s.kind] || 0) + 1; return m }, {}), written_to: debt.written_to } : null,
  ok: failed.length === 0,
  counts: { Research: { ok: okCount, failed: results.length - okCount } },
  failed,
}
log(`I.1 done: claims=${summary.claims?.count} components=${Object.values(codemap).reduce((n, c) => n + (c ? c.components : 0), 0)} debt=${summary.debt?.suspects} failed=${failed.map(f => f.label).join(',') || 'none'}`)
return summary
