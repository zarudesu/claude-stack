#!/usr/bin/env python3
"""Mutation-test every canary claim in STATUS.yaml.

Usage: python3 tools/ground_truth/gt_mutation_selfcheck.py [--root DIR] [--id CLAIM_ID]

For each canary, require a green baseline, apply mutation.find -> mutation.replace,
require a red check, restore the file and require green again. Restoration
runs in finally and signal handlers; SIGKILL or a machine crash cannot be recovered
by those handlers, so mutation runs belong in isolated working trees.

A canary whose check cannot run here (no go toolchain, no js runner, a probe
that skipped) is reported as SKIP and does not count against the run, unless
--strict is given -- CI, which does have the toolchains installed, passes it.

Exit 0 can include explicit SKIP unless --strict. Exit 1: required proof
failed or an error prevented the run. SKIP is never mutation evidence.
"""
from __future__ import annotations

import argparse
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import contract_lib  # noqa: E402


def _find_root(start: Path) -> Path:
    for candidate in [start, *start.parents]:
        if (candidate / "STATUS.yaml").is_file():
            return candidate
    return start


def _is_dirty(root: Path, rel_path: str) -> bool:
    result = subprocess.run(
        ["git", "status", "--porcelain", "--", rel_path],
        cwd=root,
        capture_output=True,
        text=True,
    )
    return bool(result.stdout.strip())


def _check_is_red(root: Path, claim: dict) -> tuple[bool, str, bool]:
    """(went_red, message, skipped) for the claim's check as it stands right
    now. The command line comes from contract_lib.run_claim_check, so a canary
    reddens under exactly the check the verifier runs -- including go_test,
    js_test and junit, which this script used to refuse outright."""
    ok, message, skipped = contract_lib.run_claim_check(root, claim)
    return (not ok and not skipped), message, skipped


def _run_one(root: Path, claim: dict, allow_dirty: bool = False, strict: bool = False) -> bool:
    cid = claim["id"]
    mutation = claim.get("mutation") or {}
    rel_file = mutation.get("file")
    find = mutation.get("find")
    replace = mutation.get("replace")
    if not rel_file or find is None or replace is None:
        print(f"[FAIL] {cid} : canary missing mutation.file/find/replace")
        return False

    if not allow_dirty and _is_dirty(root, rel_file):
        print(f"[FAIL] {cid} : {rel_file} has uncommitted changes, refusing to mutate")
        return False

    baseline_ok, message, skipped = contract_lib.run_claim_check(root, claim)
    if not baseline_ok:
        label = "SKIP" if skipped and not strict else "FAIL"
        print(f"[{label}] {cid} : baseline check is not green: {message}")
        return skipped and not strict

    full = root / rel_file
    original = full.read_text()
    if find not in original:
        print(f"[FAIL] {cid} : mutation.find {find!r} not present in {rel_file}")
        return False

    mutated = original.replace(find, replace, 1)

    def restore() -> None:
        full.write_text(original)
        # Drop any bytecode cache rewritten against the mutated source so
        # the next run (this one's own restore included) cannot pick up a
        # stale .pyc instead of the file just restored.
        shutil.rmtree(full.parent / "__pycache__", ignore_errors=True)

    def on_signal(signum, _frame):
        # A finally alone does not survive a signal: interrupted here, the
        # run would leave the mutated file on disk as the working copy.
        restore()
        os._exit(128 + signum)

    previous = {}
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        try:
            previous[sig] = signal.signal(sig, on_signal)
        except (OSError, ValueError):
            pass
    full.write_text(mutated)
    try:
        went_red, tail, skipped = _check_is_red(root, claim)
    finally:
        restore()
        for sig, handler in previous.items():
            try:
                signal.signal(sig, handler)
            except (OSError, ValueError):
                pass

    if skipped:
        print(f"[SKIP] {cid} : {tail}")
        return not strict
    if went_red:
        restored_ok, message, _ = contract_lib.run_claim_check(root, claim)
        if not restored_ok:
            print(f"[FAIL] {cid} : restored check is not green: {message}")
            return False
        print(f"[PASS] {cid} : check went red under mutation")
        return True
    print(f"[FAIL] {cid} : check stayed green under mutation (no teeth)\n{tail}")
    return False


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=None)
    parser.add_argument("--id", dest="claim_id", default=None)
    parser.add_argument("--allow-dirty", action="store_true",
                        help="mutate files with uncommitted changes too (the original bytes are restored, not git state)")
    parser.add_argument("--strict", action="store_true",
                        help="treat a canary whose check could not run here as a failure")
    args = parser.parse_args(argv)

    root = args.root or _find_root(Path(__file__).resolve().parent)
    try:
        data = contract_lib.load_contract(root)
    except contract_lib.ContractError as exc:
        print(f"[FAIL] - : {exc}")
        return 1

    canaries = [c for c in data.get("claims") or [] if isinstance(c, dict) and c.get("canary")]
    if args.claim_id is not None:
        canaries = [c for c in canaries if c.get("id") == args.claim_id]
        if not canaries:
            print(f"[FAIL] {args.claim_id} : no canary claim with that id")
            return 1

    if not canaries:
        print("[FAIL] - : no canary claims found")
        return 1

    ok = True
    for claim in canaries:
        if not _run_one(root, claim, allow_dirty=args.allow_dirty, strict=args.strict):
            ok = False
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
