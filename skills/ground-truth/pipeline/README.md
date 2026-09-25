# pipeline/

The scripted half of the ground-truth skill: a run config (`run.example.json`),
a shared library (`gt_lib/`), one python stage per step under `stages/`, one
Workflow script per multi-agent step under `workflows/`, a dispatcher
(`gt.py`) that runs a stage by name, and a consistency check
(`check_identity_sync.py`) that keeps every workflow's banned-word text in
sync with `gt_lib/banned_words.py`.

For what each stage and workflow does and in what order, see `SPEC.md`
(this packaging's file-level spec) and `../references/pipeline.md` (the
runbook).
