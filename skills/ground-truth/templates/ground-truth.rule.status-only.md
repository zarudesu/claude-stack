<!-- Installer: replace {{PY}} with this project's interpreter. Installed instead of ground-truth.rule.md while the repo has no .ground-truth/model.yaml. -->
# ground-truth: STATUS contract

STATUS.yaml indexes executable claims in its declared scope; it is not a map of
the whole project. Find the claims for the code you change with a grep
(`rg -n 'path/to/file' STATUS.yaml`), then
`{{PY}} tools/ground_truth/blast_radius.py <path-or-claim-id>` for importers and
declared relationships. Pick the claim whose concern matches the task: one file
can carry several claims with different statuses. Read the implementation you
change; do not rediscover the entire project.

After edits, run relevant tests and inspect the diff. Review effects on
consumers. Update claims only when their meaning, path or evidence changed.
A new function need not have a claim. Current code and intended requirements
can disagree: classify the discrepancy, do not legitimize a bug by silently
rewriting requirements.

New/raised implemented or partial claims require baseline green → meaningful
mutation red → restored green, in isolation. Stop does not mutate working files;
CI supplies the required proof. Skip, checker error or incomplete coverage is
not verified.

Before finishing, run the applicable tests and, when STATUS.yaml exists,
`{{PY}} tools/ground_truth/verify.py --mode=sync`. Report changed claims and
checks. Hooks never supply semantic approval.

Repeated failure: diagnose product versus checker, report after two unsuccessful
repairs, do not suppress the check. Legacy pre-edit gates remain opt-in:
GT_STRICT_EDIT_GUARD=1. This repo has no project memory (.ground-truth/model.yaml);
ground-truth init adds it.
