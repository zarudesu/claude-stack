#!/usr/bin/env python3
"""Check STATUS.yaml against the repo it describes.

Usage: python3 tools/ground_truth/verify.py --mode=sync|full [--root DIR]

Exit 0: no FAIL issues. Exit 1: at least one FAIL issue. Exit 3: STATUS.yaml
is missing or does not parse, or the check itself crashed.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import contract_lib  # noqa: E402
import gt_store  # noqa: E402


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["sync", "full"], required=True)
    parser.add_argument("--root", type=Path, default=None)
    parser.add_argument("--show-warn", action="store_true")
    args = parser.parse_args(argv)

    here = Path(__file__).resolve().parent
    root = args.root or gt_store.find_root(here) or here

    try:
        issues = contract_lib.run(root, args.mode)
    except contract_lib.ContractError as exc:
        print(f"[FAIL] - : {exc}")
        return 3

    # The deferred-drift queue may defer, not grow without bound or age. A
    # finding queued and not escalated is reported, not failed; one outside the
    # queue fails as before. Only entries this run still finds count: a fix
    # committed before Stop leaves its entry in the file. Memory entries are
    # gt_context.py check's.
    if args.mode == "full" and (Path(__file__).resolve().parent / "gt_pending.py").is_file():
        import gt_pending
        queued = gt_pending.load(root)[2]
        live = set()
        for n, issue in enumerate(issues):
            if not any(p.search(issue.message) for p in gt_pending.STATUS_CLASSES.values()):
                continue
            for cls, obj, _, _ in gt_pending.status_findings(root, [f"[WARN] {issue.claim_id} : {issue.message}"]):
                key = f"{cls}:{obj}"
                live.add(key)
                if issue.severity == "fail" and key in queued and not queued[key]["escalated"]:
                    issues[n] = contract_lib.Issue("warn", issue.claim_id, issue.message)
        issues += [contract_lib.Issue("fail", "-", p)
                   for p in gt_pending.ci_problems(root, live, set(gt_pending.STATUS_CLASSES))]

    fails = [issue for issue in issues if issue.severity == "fail"]
    warns = [issue for issue in issues if issue.severity != "fail"]
    # gt context marks a claim path changed since this green run.
    if args.mode == "sync" and not fails and (here / "gt_packet.py").is_file():
        import gt_packet
        gt_packet.write_digests(root)

    for issue in fails:
        print(f"[FAIL] {issue.claim_id} : {issue.message}")

    if args.mode == "sync" and not args.show_warn:
        if warns:
            print(
                f"[WARN] - : {len(warns)} warnings hidden in sync mode "
                "(verify.py --mode=sync --show-warn to list)"
            )
    else:
        for issue in warns:
            print(f"[WARN] {issue.claim_id} : {issue.message}")

    return 1 if fails else 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Exception as exc:  # noqa: BLE001 -- a crash must not read as "no findings"
        print(f"[FAIL] - : internal error: {type(exc).__name__}: {exc}")
        sys.exit(3)
