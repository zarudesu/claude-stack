#!/usr/bin/env python3
"""Build i4-args.json (and its per-brief files) for the I.4 code-fix workflow.

Reads claims-judged.json, i4-triage.json, i4-groups.json and
canary-candidates.json, then writes:

- run.research/i4/probe/pgNN.json, one per probe group (chunks of 3,
  sorted by component then claim id, so a batch stays close to one part
  of the codebase)
- run.research/i4/test/tgNN.json, one per test file that needs new tests
- run.research/i4/canary/<claim_id>.json, one per canary claim
- run.scratch/i4/i4-args.json, the workflow's args: repo, python,
  tmp_base, brief_dir, a path to the Classify phase's brief list, and the
  probe/test/canary groups both inline (i4_apply reads claim data straight
  out of this file) and as the brief_file paths above (the workflow's
  agents Read those instead of carrying the same JSON through every
  prompt).

Canary claim ids always come from canary-candidates.json (written by
i35_assemble_status); --canaries caps how many are used, in file order.

This port drops the brief_class/prod_files_forbidden fields the last run's
build script attached from a prior classify pass: D12 forbids production
edits in I.4 for every brief regardless of class, so nothing here needs to
branch on it, and gating it out removes an in-scope-vs-out-of-scope
distinction that the workflow rules already make redundant.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from gt_lib.paths import args_path, load_run, research, stage_dir  # noqa: E402

PROBE_CHUNK = 3


def base(cid: str, judged_by_id: dict, draft_by_id: dict) -> dict:
    j = judged_by_id[cid]
    dr = draft_by_id.get(cid, {})
    return {
        "claim_id": cid,
        "component": dr.get("component", ""),
        "status": j.get("final_status"),
        "path": j.get("final_path") or [],
        "check": j.get("final_check"),
        "note": j.get("final_note") or "",
        "brief": j.get("code_fix_brief") or "",
        "judge_reason": j.get("reason") or "",
        "was_red_plan": dr.get("was_check_ever_red_plan") or "",
    }


def build_probe_groups(probes: list[dict], judged_by_id: dict, draft_by_id: dict, out_dir: Path):
    ordered = sorted(
        probes,
        key=lambda p: (draft_by_id.get(p["claim_id"], {}).get("component", ""), p["claim_id"]),
    )
    groups = []
    for i in range(0, len(ordered), PROBE_CHUNK):
        chunk = ordered[i : i + PROBE_CHUNK]
        gid = f"pg{len(groups) + 1:02d}"
        entries = [dict(base(p["claim_id"], judged_by_id, draft_by_id), probe_file=p["path"]) for p in chunk]
        brief_file = out_dir / f"{gid}.json"
        brief_file.write_text(json.dumps(entries, indent=2, ensure_ascii=False), encoding="utf-8")
        groups.append({"id": gid, "brief_file": str(brief_file), "probes": entries})
    return groups


def build_test_groups(tests_by_file: dict, required_by_id: dict, judged_by_id: dict, draft_by_id: dict, repo: Path, out_dir: Path):
    groups = []
    for f, cids in sorted(tests_by_file.items()):
        gid = f"tg{len(groups) + 1:02d}"
        claims = [
            dict(base(c, judged_by_id, draft_by_id), missing_nodes=required_by_id[c]["missing"])
            for c in cids
        ]
        first_nodes = [required_by_id[c]["missing"][0] for c in cids]
        exists = (repo / f).is_file()
        payload = {"test_file": f, "exists": exists, "first_nodes": first_nodes, "claims": claims}
        brief_file = out_dir / f"{gid}.json"
        brief_file.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        groups.append({
            "id": gid, "test_file": f, "exists": exists, "first_nodes": first_nodes,
            "brief_file": str(brief_file), "claims": claims,
        })
    return groups


def build_canaries(candidates: list[dict], count: int | None, judged_by_id: dict, draft_by_id: dict, out_dir: Path):
    picked = candidates if count is None else candidates[:count]
    canaries = []
    for c in picked:
        cid = c["claim_id"]
        if cid not in judged_by_id:
            print(f"warning: canary claim {cid} not found in claims-judged.json, skipping", file=sys.stderr)
            continue
        mutation = c.get("mutation_suggestion") or judged_by_id[cid].get("mutation_suggestion") or ""
        entry = dict(base(cid, judged_by_id, draft_by_id), mutation_suggestion=mutation)
        brief_file = out_dir / f"{cid}.json"
        brief_file.write_text(json.dumps(entry, indent=2, ensure_ascii=False), encoding="utf-8")
        canaries.append(dict(entry, brief_file=str(brief_file)))
    return canaries


def check_d24(test_groups: list[dict]) -> None:
    """D24: exactly one new pytest function per claim. Flag a file where two claims share a node id."""
    for g in test_groups:
        n_claims, n_nodes = len(g["claims"]), len(set(g["first_nodes"]))
        if n_claims != n_nodes:
            print(f"D24 WARNING: {g['test_file']} has {n_claims} claims but only {n_nodes} distinct node ids")
        else:
            print(f"D24 ok: {g['test_file']} claims={n_claims} nodes={n_nodes}")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Build i4-args.json and its per-brief files for the I.4 code-fix workflow.",
    )
    parser.add_argument("--run", required=True, help="path to run.json")
    parser.add_argument("--canaries", type=int, default=None, help="cap the number of canary claims used")
    parser.add_argument("--models", help="path to a JSON file of per-role model overrides")
    parser.add_argument(
        "--i35-dir",
        help="directory holding canary-candidates.json (default: run.scratch/i35, i35_assemble_status's own default --out-dir)",
    )
    args = parser.parse_args(argv)

    run = load_run(args.run)

    judged_data = json.loads((run.research / "claims-judged.json").read_text(encoding="utf-8"))
    judged_by_id = {j["claim_id"]: j for j in judged_data.get("judged") or [] if not j.get("merged_into")}
    draft_by_id = {
        it["draft"]["id"]: it["draft"]
        for it in judged_data.get("items") or [] if (it.get("draft") or {}).get("id")
    }

    triage = json.loads((run.research / "i4-triage.json").read_text(encoding="utf-8"))
    groups_data = json.loads((run.research / "i4-groups.json").read_text(encoding="utf-8"))
    required_by_id = {
        r["claim_id"]: r
        for r in (triage.get("required") or []) + (triage.get("optional") or [])
    }

    # i35_assemble_status writes canary-candidates.json under its --out-dir,
    # which defaults to run.scratch/i35; pass --i35-dir here when that stage
    # was run with a non-default --out-dir, or canaries silently comes out 0.
    i35_dir = Path(args.i35_dir) if args.i35_dir else run.scratch / "i35"
    canary_path = i35_dir / "canary-candidates.json"
    if canary_path.exists():
        candidates = json.loads(canary_path.read_text(encoding="utf-8"))
    else:
        candidates = []
        print(
            f"warning: {canary_path} not found -- 0 canaries will be built; "
            "pass --i35-dir if i35_assemble_status used a non-default --out-dir",
            file=sys.stderr,
        )

    classify_path = stage_dir(run, "i4") / "classify-briefs.json"
    if not classify_path.exists():
        parser.error(f"{classify_path} not found -- run i4_classify_briefs first")

    probe_dir = research(run, "i4", "probe")
    test_dir = research(run, "i4", "test")
    canary_dir = research(run, "i4", "canary")
    for d in (probe_dir, test_dir, canary_dir):
        d.mkdir(parents=True, exist_ok=True)

    probe_groups = build_probe_groups(triage.get("probes") or [], judged_by_id, draft_by_id, probe_dir)
    test_groups = build_test_groups(
        groups_data.get("tests_by_file") or {}, required_by_id, judged_by_id, draft_by_id, run.repo, test_dir,
    )
    canaries = build_canaries(candidates, args.canaries, judged_by_id, draft_by_id, canary_dir)

    tmp_base = stage_dir(run, "i4") / "tmp"
    tmp_base.mkdir(parents=True, exist_ok=True)

    out = {
        "repo": str(run.repo),
        "python": str(run.python),
        "tmp_base": str(tmp_base),
        "brief_dir": str(research(run, "i4")),
        "classify": str(classify_path),
        "probe_groups": probe_groups,
        "test_groups": test_groups,
        "canaries": canaries,
    }
    if args.models:
        out["models"] = json.loads(Path(args.models).read_text(encoding="utf-8"))

    out_path = args_path(run, "i4")
    out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")

    n_probes = sum(len(g["probes"]) for g in probe_groups)
    n_test_claims = sum(len(g["claims"]) for g in test_groups)
    print(
        f"wrote {out_path}: probe groups={len(probe_groups)} ({n_probes} probes) "
        f"test groups={len(test_groups)} ({n_test_claims} claims) canaries={len(canaries)}"
    )
    check_d24(test_groups)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
