#!/usr/bin/env python3
"""Close an audit: set meta.last_audited and meta.last_audited_commit.

last_audited becomes run.date; last_audited_commit becomes the full HEAD
sha of run.repo -- the commit the audit actually looked at, so a later
audit_scope can compare against it. Requires explicit --reviewed and a
fresh full verification. --force allows initial review without a scope
file; it never bypasses verification. An existing scope must match HEAD.
gt_store.write_meta rewrites only those two lines of the file holding
meta (STATUS.yaml or .ground-truth/contract.yaml) and keeps comments.
Writes only with --write; the caller attests semantic review.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "templates"))
import gt_store  # noqa: E402
from gt_lib.git import _run  # noqa: E402
from gt_lib.paths import load_run, stage_dir  # noqa: E402
from stages.audit_scope import verify  # noqa: E402


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run", required=True)
    parser.add_argument("--status", help="path to the file holding meta (default: the one of run.repo's layout)")
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--reviewed", action="store_true", help="confirm substantive review of the current scope")
    parser.add_argument("--force", action="store_true", help="allow initial audit with no scope report; does not bypass verification")
    args = parser.parse_args(argv)
    if not args.reviewed:
        print("refusing: a quick check is not an audit; --reviewed requires substantive review of the current scope")
        return 1
    run = load_run(args.run)
    meta_name = (gt_store.contract_files(run.repo) or [gt_store.STATUS])[0]
    status_path = Path(args.status) if args.status else run.repo / meta_name

    scope_path = stage_dir(run, "audit") / "audit-scope.json"
    if not args.force:
        if not scope_path.exists():
            print(f"refusing: {scope_path} missing (run audit_scope first, or --force)"); return 1
        scope = json.loads(scope_path.read_text(encoding="utf-8"))
        if scope.get("head") != _run(run.repo, ["rev-parse", "HEAD"]).strip():
            print("refusing: HEAD changed since audit_scope; refresh the scope and review the new changes"); return 1

    if status_path.resolve() != (run.repo / meta_name).resolve():
        print(f"refusing: only the repository {meta_name} can receive the audit checkpoint"); return 1
    current = verify(run)
    if current["exit"] != 0:
        print(f"refusing: current verification failed (exit {current['exit']}, {current['fail']} failures)")
        return 1

    meta = gt_store.load(run.repo).data.get("meta") or {}
    if "last_audited" not in meta:
        print("no meta.last_audited line found"); return 1
    updates = {"last_audited": run.date, "last_audited_commit": _run(run.repo, ["rev-parse", "HEAD"]).strip()}
    for key, value in updates.items():
        print(f"{key}: {meta.get(key, '(none)')}  ->  {key}: {value}")

    changed = any(str(meta.get(key)) != value for key, value in updates.items())
    if args.write and changed:
        gt_store.write_meta(run.repo, updates)
        print("written")
    elif not args.write:
        print("dry-run, nothing written")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
