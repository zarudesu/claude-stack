<!-- Installer: replace {{PY}} with this project's interpreter. -->
# Project memory: use, change, hand off

Start with `{{PY}} tools/ground_truth/gt_context.py list`, choose the task's
areas, then `{{PY}} tools/ground_truth/gt_context.py context --area ID`.
Use only a fresh, complete packet for trusted context. It contains canonical
knowledge, dependencies, consumers, reusable entrypoints, checks and unknowns.
Read the implementation you change; do not rediscover the entire project.
No model, missing checkout, stale evidence or incomplete packet is not readiness.

PM handoff includes the goal/acceptance, actual workspace/checkout, area IDs,
revision, packet or retrieval command, constraints, unknowns and required checks.
A new worker obtains current context in its own checkout, not the PM's unverified
summary. Another worktree requires a correctly bound task-local model.

After edits, run relevant tests and inspect the diff. Maintain the canonical
facts and dependency edges that changed; do not duplicate knowledge or rewrite
unaffected docs. Review effects on consumers, including across repositories.
Packets also name co_owned_areas: shared inputs may require review of those
owners even without a dependency edge. Select actual changed owners from status.
A new function need not have a claim, but its boundary/route must not be missing.
Current code and intended requirements can disagree: classify the discrepancy,
do not legitimize a bug by silently rewriting requirements.

Run `{{PY}} tools/ground_truth/gt_context.py status`. After semantic review
of each affected area and its dependency changes, record the current revision:
`{{PY}} tools/ground_truth/gt_context.py refresh --area ID --reviewed --expect REV --evidence "actual review and checks"`.
Repeat --area only for reviewed areas. Never auto-refresh, hand-edit receipts,
or use --all merely to clear stale. Unchanged semantics may need review but
not a prose edit. An input change after review requires a new delta check.

Before finishing, obtain the fresh task packet, run the applicable tests,
`{{PY}} tools/ground_truth/gt_context.py check`, and, when STATUS.yaml exists,
`{{PY}} tools/ground_truth/verify.py --mode=sync`.
Global stale from independent work is an explicit limitation, not evidence that
your task failed or the whole workspace is ready. Report changed areas, revision,
checks and unresolved dependencies. Hooks never supply semantic approval.

STATUS.yaml indexes executable claims in its declared scope. New/raised
implemented or partial claims require baseline green → meaningful mutation red
→ restored green, in isolation. Stop does not mutate working files; CI supplies
the required proof. Skip, checker error or incomplete coverage is not verified.
Update claims only when their meaning, path or evidence changed.

CI checks memory freshness separately from behavior. Hash equality is not a
proof of truthful prose, all runtime relationships or production state.
Repeated failure: diagnose product versus checker, report after two unsuccessful
repairs, do not suppress the check. Full init is thorough once; ordinary sync
is local. Legacy pre-edit gates remain opt-in: GT_STRICT_EDIT_GUARD=1.
