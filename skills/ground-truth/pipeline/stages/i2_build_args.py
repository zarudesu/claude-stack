#!/usr/bin/env python3
"""Build the args file for the I.2-I.3.5 workflow.

Reads run.research/slices-index.json (written by i2_build_slices) and
writes run.scratch/i2/i2-args.json, the {repo, research, slices, python,
models?} object the ground-truth-i2-i35 workflow expects as its `args`
input.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from gt_lib.paths import args_path, load_run  # noqa: E402


def build(run, models_path: str | None) -> dict:
    index_path = run.research / "slices-index.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))

    payload = {
        "repo": str(run.repo),
        "research": str(run.research),
        "slices": index,
        "python": str(run.python),
    }
    if models_path:
        payload["models"] = json.loads(Path(models_path).read_text(encoding="utf-8"))

    return payload


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Build the args file for the I.2-I.3.5 workflow.")
    parser.add_argument("--run", required=True, help="path to run.json")
    parser.add_argument("--models", help="path to a JSON file with role model overrides")
    args = parser.parse_args(argv)

    run = load_run(args.run)
    payload = build(run, args.models)

    out_path = args_path(run, "i2")
    out_path.write_text(json.dumps(payload, indent=1, ensure_ascii=False), encoding="utf-8")

    n_slices = len(payload["slices"])
    n_claims = sum(s.get("n_claims", 0) for s in payload["slices"])
    n_debt = sum(s.get("n_debt", 0) for s in payload["slices"])
    print(f"wrote {out_path}")
    print(f"slices={n_slices} claims={n_claims} debt={n_debt}")

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
