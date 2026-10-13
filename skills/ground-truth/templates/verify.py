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


def _find_root(start: Path) -> Path:
    for candidate in [start, *start.parents]:
        if (candidate / "STATUS.yaml").is_file():
            return candidate
    return start


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["sync", "full"], required=True)
    parser.add_argument("--root", type=Path, default=None)
    parser.add_argument("--show-warn", action="store_true")
    args = parser.parse_args(argv)

    root = args.root or _find_root(Path(__file__).resolve().parent)

    try:
        issues = contract_lib.run(root, args.mode)
    except contract_lib.ContractError as exc:
        print(f"[FAIL] - : {exc}")
        return 3

    # The deferred-drift queue may defer, not grow without bound or age. Only
    # entries this run still finds count: a fix committed before Stop leaves its
    # entry in the file. Memory entries are counted as they are.
    if args.mode == "full" and (Path(__file__).resolve().parent / "gt_pending.py").is_file():
        import gt_pending
        lines = [f"[WARN] {issue.claim_id} : {issue.message}" for issue in issues]
        live = {f"{cls}:{obj}" for cls, obj, _, _ in gt_pending.status_findings(root, lines)}
        issues += [contract_lib.Issue("fail", "-", p)
                   for p in gt_pending.ci_problems(root, live, set(gt_pending.STATUS_CLASSES))]

    fails = [issue for issue in issues if issue.severity == "fail"]
    warns = [issue for issue in issues if issue.severity != "fail"]

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
