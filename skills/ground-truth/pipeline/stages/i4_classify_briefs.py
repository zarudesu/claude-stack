#!/usr/bin/env python3
"""Deterministic I.4 triage: split code-fix-bearing claims into probe/test/skip buckets.

A claim is in scope here once it is judged, not merged into another claim,
and carries a non-empty code_fix_brief -- regardless of route, since a
DOC_FIX_ONLY claim can still need a probe or test written before its doc
edit is trustworthy. For each in-scope claim:

- final_check_kind == "probe": the claim needs a probe written unless the
  probe file already exists in the worktree.
- final_check_kind == "pytest": final_check is a ";"-separated list of
  pytest node ids. Existence is checked with an AST scan (no pytest run,
  no venv needed) of top-level test_* functions and Test* classes' test_*
  methods. Zero existing nodes -> the claim is "required" (a test group
  must be built for it). Some existing, some missing -> "optional" (noted,
  not built -- a known scope gap, see below). All existing -> the claim is
  already satisfied and drops out of I.4 entirely.
- any other kind (js_test, go_test, junit, n/a, ...) -> also dropped,
  counted as a skipped kind. I.4 in this port only builds pytest test
  groups and probes; a js/go/junit test-writing phase is out of scope
  here.

This stage writes run.research/i4-triage.json ({required, optional,
probes}) and run.research/i4-groups.json ({tests_by_file}, required
claims only, grouped by the file of each claim's first missing node id)
plus the narrower brief list i4_build_args and i4.js's Classify phase
consume: run.scratch/i4/classify-briefs.json, restricted to
route == "CODE_FIX_CANDIDATE" claims with a brief (the DOC_FIX_ONLY+brief
claims above are in the broader triage scope but not classified -- they
never reach production code, so there is nothing to classify).

Classification itself (test_only/probe_only/mixed/prod_change,
self_inconsistency, prod_files) is not done here: on the last run those
labels came from a single agent pass over the CODE_FIX briefs, so they
stay agent-produced -- that pass is the Classify phase of i4.js (role
codefix), and i4_apply folds its output into run.research/i4-brief-classes
.json. Class labels are never an input to brief building: D12 forbids
production edits in I.4 for every brief regardless of class, so nothing
downstream of this stage branches on class.
"""
from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from gt_lib.paths import load_run, stage_dir  # noqa: E402
from gt_lib.pytest_nodes import pytest_nodes_in_file  # noqa: E402


def triage_claim(j: dict, repo: Path, skipped: collections.Counter):
    """Classify one in-scope judged claim into (required, optional, probe) entries, or None."""
    cid = j["claim_id"]
    kind = j.get("final_check_kind") or "n/a"
    check = j.get("final_check") or ""

    if kind == "probe":
        if check and (repo / check).is_file():
            return None
        return "probe", {"claim_id": cid, "status": j.get("final_status"), "path": check}

    if kind == "pytest":
        nodes = [n.strip() for n in check.split(";") if n.strip()]
        if not nodes:
            skipped["pytest-empty"] += 1
            return None
        existing, missing = [], []
        for n in nodes:
            if "::" not in n:
                missing.append(n)
                continue
            file_part, node_part = n.split("::", 1)
            fpath = repo / file_part
            if fpath.is_file() and node_part in pytest_nodes_in_file(fpath):
                existing.append(n)
            else:
                missing.append(n)
        if not existing:
            return "required", {"claim_id": cid, "status": j.get("final_status"), "missing": missing}
        if missing:
            return "optional", {
                "claim_id": cid, "status": j.get("final_status"),
                "existing": existing, "missing": missing,
            }
        return None

    skipped[kind] += 1
    return None


def build_groups(required: list[dict]) -> dict:
    """Group required claims by the file of their first missing pytest node id."""
    tests_by_file = collections.defaultdict(list)
    for r in required:
        first_file = r["missing"][0].split("::", 1)[0]
        tests_by_file[first_file].append(r["claim_id"])
    return dict(tests_by_file)


def build_brief(j: dict, draft_by_id: dict) -> dict:
    dr = draft_by_id.get(j["claim_id"], {})
    return {
        "claim_id": j["claim_id"],
        "component": dr.get("component", ""),
        "status": j.get("final_status"),
        "path": j.get("final_path") or [],
        "check": j.get("final_check"),
        "note": j.get("final_note") or "",
        "brief": j.get("code_fix_brief") or "",
        "judge_reason": j.get("reason") or "",
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Split code-fix-bearing judged claims into I.4 probe/test triage buckets.",
    )
    parser.add_argument("--run", required=True, help="path to run.json")
    args = parser.parse_args(argv)

    run = load_run(args.run)
    judged_path = run.research / "claims-judged.json"
    if not judged_path.exists():
        parser.error(f"{judged_path} not found -- run i35_dump_judged first")
    data = json.loads(judged_path.read_text(encoding="utf-8"))
    judged = data.get("judged") or []
    items = data.get("items") or []
    draft_by_id = {
        it["draft"]["id"]: it["draft"]
        for it in items if (it.get("draft") or {}).get("id")
    }

    in_scope = [j for j in judged if not j.get("merged_into") and j.get("code_fix_brief")]

    required, optional, probes = [], [], []
    skipped = collections.Counter()
    for j in in_scope:
        result = triage_claim(j, run.repo, skipped)
        if result is None:
            continue
        label, entry = result
        {"required": required, "optional": optional, "probe": probes}[label].append(entry)

    triage = {"required": required, "optional": optional, "probes": probes}
    # optional = check names existing nodes plus judge-added ones; the added
    # nodes are still authored, else STATUS.yaml carries phantom check ids.
    groups = {"tests_by_file": build_groups(required + optional)}
    (run.research / "i4-triage.json").write_text(json.dumps(triage, indent=2, ensure_ascii=False), encoding="utf-8")
    (run.research / "i4-groups.json").write_text(json.dumps(groups, indent=2, ensure_ascii=False), encoding="utf-8")

    classify_scope = [
        j for j in judged
        if not j.get("merged_into") and j.get("route") == "CODE_FIX_CANDIDATE" and j.get("code_fix_brief")
    ]
    briefs = sorted((build_brief(j, draft_by_id) for j in classify_scope), key=lambda b: b["claim_id"])
    briefs_dir = stage_dir(run, "i4")
    (briefs_dir / "classify-briefs.json").write_text(json.dumps(briefs, indent=2, ensure_ascii=False), encoding="utf-8")

    print(
        f"in-scope claims={len(in_scope)} required={len(required)} optional={len(optional)} "
        f"probes={len(probes)} test files={len(groups['tests_by_file'])} classify-briefs={len(briefs)}"
    )
    if skipped:
        print(f"skipped (not built by this port): {dict(skipped)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
