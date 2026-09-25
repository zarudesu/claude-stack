# status-contract fixture

Tiny multi-language sample repo (Python, Go, JS) with a known ground truth,
used to check a STATUS.yaml-producing pipeline against reality.

The docs inside repo/ deliberately lie: JSON export, emailed notifications
and a nightly scheduler that don't work or exist, plus a claim of "no
secrets" despite app/config.py holding one.

Run `./make_fixture.sh <target-dir>` to copy repo/ into a fresh one-commit
git repo. Compare the pipeline's STATUS.yaml against expected.json: per-file
status/check_kind, required debt entries, the docs' false claims, the
minimum canary count, and the coverage roots.
