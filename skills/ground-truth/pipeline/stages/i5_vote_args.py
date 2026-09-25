#!/usr/bin/env python3
"""Build the args file for the raise-vote workflow.

Reads run.research/raise-candidates.json (written by i5_recon_apply.py),
resolves each claim id's check/docs/code from STATUS.yaml, and writes
run.scratch/vote/vote-args.json for pipeline/workflows/raise-vote.js.

docs/code are read from the claim's own STATUS.yaml note: every
path:N-style anchor named there is sorted by extension -- .md/.yaml/.yml/
.example anchors go to "docs", everything else to "code". This is a
best-effort reconstruction -- the source run built vote-args.json by hand
for a small, chosen set of claims; here every raise candidate the recon
judge upheld gets an entry.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from gt_lib import paths  # noqa: E402
from gt_lib.paths import load_run  # noqa: E402
from gt_lib.yaml_edit import blocks, get_field  # noqa: E402

ANCHOR_RE = re.compile(r"(?<![\w/.-])((?:[\w.-]+/)*[\w.-]+\.[\w.-]+):(\d+)(?:-(\d+))?")


def unquote(s):
    """Undo yaml_edit.scalar()'s json.dumps quoting for a raw get_field() value.

    get_field() returns the regex-captured text of a single-line scalar
    as-is; when the source line was quoted (STATUS.yaml.template quotes
    every check/note example, and a real check id or note can need it --
    a pytest node id with a bracketed parameter contains ": ") that text
    still carries its surrounding quote characters and JSON escapes.
    """
    if s is not None and s[:1] == '"' and s[-1:] == '"':
        try:
            return json.loads(s)
        except (json.JSONDecodeError, IndexError):
            return s
    return s


def paths_cited(note: str) -> list[str]:
    seen = []
    for m in ANCHOR_RE.finditer(note or ""):
        p = m.group(1)
        if p not in seen:
            seen.append(p)
    return seen


def build(run, status_path: Path, candidates: list[dict], models) -> dict:
    t = status_path.read_text(encoding="utf-8")
    E = blocks(t)
    claims = []
    skipped = []
    for c in candidates:
        i = c["id"]
        if i not in E:
            skipped.append(i)
            continue
        check = unquote(get_field(t, i, "check"))
        note = unquote(get_field(t, i, "note"))
        if check is None or note is None:
            skipped.append(i)
            continue
        cited = paths_cited(note)
        docs = [p for p in cited if p.endswith((".md", ".yaml", ".yml", ".example"))]
        code = [p for p in cited if p not in docs]
        claims.append({"id": i, "check": check, "docs": docs, "code": code})

    out = {"repo": str(run.repo), "python": str(run.python), "date": run.date, "claims": claims}
    if models is not None:
        out["models"] = models
    return {"args": out, "skipped": skipped}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Build the raise-vote workflow args file.")
    parser.add_argument("--run", required=True, help="path to run.json")
    parser.add_argument("--candidates", help="path to raise-candidates.json (default: run.research/raise-candidates.json)")
    parser.add_argument("--status", help="path to STATUS.yaml (default: run.repo/STATUS.yaml)")
    parser.add_argument("--models", help="path to a JSON file of per-role model overrides")
    args = parser.parse_args(argv)

    run = load_run(args.run)

    cand_path = Path(args.candidates) if args.candidates else run.research / "raise-candidates.json"
    if not cand_path.is_file():
        print(f"error: raise-candidates.json not found at {cand_path}", file=sys.stderr)
        return 1
    candidates = json.loads(cand_path.read_text(encoding="utf-8"))

    status_path = Path(args.status) if args.status else run.repo / "STATUS.yaml"
    if not status_path.is_file():
        print(f"error: STATUS.yaml not found at {status_path}", file=sys.stderr)
        return 1

    models = None
    if args.models:
        models = json.loads(Path(args.models).read_text(encoding="utf-8"))

    result = build(run, status_path, candidates, models)

    out_dir = paths.stage_dir(run, "vote")
    out_path = out_dir / "vote-args.json"
    out_path.write_text(json.dumps(result["args"], indent=1, ensure_ascii=False), encoding="utf-8")

    print(f"claims={len(result['args']['claims'])} -> {out_path}")
    if result["skipped"]:
        print(f"warning: {len(result['skipped'])} candidate id(s) not found in STATUS.yaml: {result['skipped']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
