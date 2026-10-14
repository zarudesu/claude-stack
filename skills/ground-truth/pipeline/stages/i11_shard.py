#!/usr/bin/env python3
"""I.11 shard: move a new install's STATUS.yaml into .ground-truth/ with gt migrate --write.

Runs the installed tools/ground_truth/gt.py migrate --write --skill <run.skill>
in run.repo after I.10; the contract must be committed first, and the
change it stages is committed as one change. --legacy-layout skips the
step and keeps STATUS.yaml. Any non-zero exit of gt migrate (3: a repo
probe still reads STATUS.yaml, uncommitted contract, tools older than the
skill; 1: not equivalent, rolled back) fails the stage with that exit code.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from gt_lib.paths import load_run  # noqa: E402


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run", required=True, help="path to run.json")
    parser.add_argument("--legacy-layout", action="store_true", help="keep STATUS.yaml, do not migrate")
    args = parser.parse_args(argv)
    if args.legacy_layout:
        print("SKIP shard: --legacy-layout keeps STATUS.yaml")
        return 0
    run = load_run(args.run)
    rc = subprocess.run([str(run.python), "tools/ground_truth/gt.py", "migrate", "--write", "--skill", str(run.skill)],
                        cwd=run.repo).returncode
    if rc:
        print(f"FAIL shard: gt migrate --write exited {rc}; fix what it printed above and run i11_shard again, "
              "or pass --legacy-layout to keep STATUS.yaml", file=sys.stderr)
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
