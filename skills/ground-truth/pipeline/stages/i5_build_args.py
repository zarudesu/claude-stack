#!/usr/bin/env python3
"""Build the args file for the I.5 doc/comment reconciliation workflow.

Docs are every tracked *.md file under run.repo except docs/_human/**.
Areas are the coverage roots listed in STATUS.yaml's meta.coverage.roots.
Each gets a brief_file pointing at the I.5 conflict brief that
i5_build_briefs wrote for it (run.research/i5/docs/<slug>.json for a
document, run.research/i5/code/<slug>.json for an area). A missing brief
is an error; an area whose brief lists no files is left out (no agent
has anything to edit there); a doc keeps its classifier run regardless
and carries n_conflicts so the workflow can skip the edit loop when it
is zero.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from gt_lib import paths  # noqa: E402
from gt_lib.git import ls_files  # noqa: E402
from gt_lib.paths import load_run  # noqa: E402

DOC_WARN_THRESHOLD = 60


def slugify(rel_path: str) -> str:
    """Turn a repo-relative path into a filesystem/identifier-safe slug."""
    s = rel_path.lstrip(".").lower()
    s = re.sub(r"[^a-z0-9]+", "_", s)
    return s.strip("_")


def build(run, status_path: Path, models, run_arg: str) -> dict:
    md_files = [p for p in ls_files(run.repo, "*.md") if not p.startswith("docs/_human/")]
    docs = []
    for p in sorted(md_files):
        bf = paths.research(run, "i5", "docs", f"{slugify(p)}.json")
        if not bf.is_file():
            raise SystemExit(f"error: brief {bf} missing -- run i5_build_briefs first")
        brief = json.loads(bf.read_text(encoding="utf-8"))
        docs.append({"doc": p, "slug": slugify(p), "brief_file": str(bf), "n_conflicts": len(brief.get("conflicts") or [])})

    status = yaml.safe_load(status_path.read_text(encoding="utf-8")) or {}
    roots = ((status.get("meta") or {}).get("coverage") or {}).get("roots") or []
    areas = []
    for a in roots:
        bf = paths.research(run, "i5", "code", f"{slugify(a)}.json")
        if not bf.is_file():
            raise SystemExit(f"error: brief {bf} missing -- run i5_build_briefs first")
        brief = json.loads(bf.read_text(encoding="utf-8"))
        if not brief.get("files"):
            print(f"area {a}: no comment/docstring conflicts, left out")
            continue
        areas.append({"area": a, "slug": slugify(a), "brief_file": str(bf)})

    args_out = {
        "repo": str(run.repo),
        "python": str(run.python),
        "guard": f"{run.python} {run.skill}/pipeline/gt.py i5_comment_guard --run {run_arg}",
        "docs": docs,
        "areas": areas,
    }
    if models is not None:
        args_out["models"] = models
    return args_out


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Build the I.5 doc/comment reconciliation args file.")
    parser.add_argument("--run", required=True, help="path to run.json")
    parser.add_argument("--status", help="path to STATUS.yaml (default: run.repo/STATUS.yaml)")
    parser.add_argument("--models", help="path to a JSON file of per-role model overrides")
    args = parser.parse_args(argv)

    run = load_run(args.run)

    status_path = Path(args.status) if args.status else run.repo / "STATUS.yaml"
    if not status_path.is_file():
        print(f"error: STATUS.yaml not found at {status_path}", file=sys.stderr)
        return 1

    models = None
    if args.models:
        models = json.loads(Path(args.models).read_text(encoding="utf-8"))

    result = build(run, status_path, models, str(Path(args.run).resolve()))  # agents run from another cwd

    out_path = paths.args_path(run, "i5")
    out_path.write_text(json.dumps(result, indent=1, ensure_ascii=False), encoding="utf-8")

    n = len(result["docs"])
    print(f"docs={n} areas={len(result['areas'])} -> {out_path}")
    if n > DOC_WARN_THRESHOLD:
        print(
            f"warning: {n} documents exceeds {DOC_WARN_THRESHOLD}; run the review phase as a "
            "second workflow (D39) rather than one oversized run",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
