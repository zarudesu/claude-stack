#!/usr/bin/env python3
"""Dump the I.2-I.3.5 workflow result to claims-judged.json and print the I.4 readiness report.

Prefers a task-output file (the Workflow tool's <taskId>.output); falls back
to the workflow's own journal.jsonl, reconstructing the judged list from
result payloads that carry both "results" and "batch_defects_fixed" (the
shape a judge batch returns -- see gt_lib.journal). Either source lands in
run.research/claims-judged.json unchanged, then the report below checks the
dump against the repository worktree so the I.4 code-fix scope is visible
before any further stage runs.
"""
from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from gt_lib.journal import journal_path, read_journal, results_where, task_output  # noqa: E402
from gt_lib.paths import load_run  # noqa: E402
from gt_lib.pytest_nodes import pytest_nodes_in_file  # noqa: E402

# The six keys claims-judged.json carries, regardless of which source fed
# load_result: a task-output result also carries the workflow's common
# ok/counts/failed keys (see gt_lib.journal.task_output), dropped here so
# the file has one stable shape whether it came from a task output or the
# journal fallback.
JUDGED_KEYS = ("items", "judged", "batch_defects", "failed_components", "failed_batches", "routes")


def load_result(args) -> dict:
    """Return the workflow result dict, task-output preferred, journal fallback."""
    res = None
    if args.task_output:
        res = task_output(args.task_output)
    if res is None:
        if args.journal:
            jpath = Path(args.journal)
        else:
            jpath = journal_path(args.session_dir, args.workflow_id)
        print(f"no task output, falling back to journal: {jpath}", file=sys.stderr)
        entries = read_journal(jpath)
        batches = [r for r in results_where(entries, "results") if "batch_defects_fixed" in r]
        judged = [x for b in batches for x in b["results"]]
        defects = [x for b in batches for x in b["batch_defects_fixed"]]
        # items carry the component name the assembler puts on each claim;
        # a reconcile result is {component, claims, doc_conflicts}. The
        # votes are not re-attached here (the journal does not say which
        # defender/prosecutor result belongs to which draft), so the report's
        # vote counts read as zero on this path -- the task output has them.
        recon = [r for r in results_where(entries, "claims") if "component" in r]
        items = [
            {"draft": c, "three_vote": None, "defender": None, "prosecutor": None,
             "doc_conflicts": r.get("doc_conflicts") or [], "component": r["component"]}
            for r in recon for c in r.get("claims") or []
        ]
        print(f"journal fallback: items rebuilt from {len(recon)} reconcile result(s) without votes; "
              f"pass --task-output for the full shape", file=sys.stderr)
        res = {
            "items": items, "judged": judged, "batch_defects": defects,
            "failed_components": [], "failed_batches": [], "routes": {},
        }
    return {k: res.get(k, {} if k == "routes" else []) for k in JUDGED_KEYS}


# Kinds whose presence we can actually verify against the worktree. Any
# other kind (js_test, go_test, junit, n/a, ...) is counted but not checked -- I.4
# in this port only writes probes and pytest tests, so there is no scan
# for the rest and reporting "0/N present" for them would assert absence
# for checks that were simply never looked at.
CHECKABLE_KINDS = ("probe", "pytest")


def check_inventory(judged: list[dict], repo: Path) -> None:
    """Print how many of the judged checks already exist in the worktree, by kind."""
    by_kind = collections.Counter()
    present = collections.Counter()
    for j in judged:
        if j.get("merged_into"):
            continue
        kind = j.get("final_check_kind") or "n/a"
        by_kind[kind] += 1
        check = j.get("final_check") or ""
        if kind == "probe":
            if (repo / check).is_file():
                present[kind] += 1
        elif kind == "pytest":
            nodes = [n for n in check.split(";") if n.strip()]
            if nodes and all(
                "::" in n and (repo / n.split("::", 1)[0]).is_file()
                and n.split("::", 1)[1] in pytest_nodes_in_file(repo / n.split("::", 1)[0])
                for n in nodes
            ):
                present[kind] += 1
    print("check inventory against the worktree (by check_kind):")
    for kind, total in sorted(by_kind.items()):
        if kind in CHECKABLE_KINDS:
            print(f"  {kind}: {present.get(kind, 0)}/{total} already present")
        else:
            print(f"  {kind}: {total} (presence not checked)")


def normalize_merged_into(res: dict) -> list[str]:
    """Clear merged_into where the judge pointed a claim at itself.

    merged_into is meant to be empty on every surviving claim and to carry
    the survivor's id only on the absorbed one; a self-reference is a judge
    slip that would make the assembler drop the claim. Records each fix in
    res["batch_defects"] so the report keeps the trace.
    """
    fixed: list[str] = []
    for x in res.get("judged") or []:
        if x.get("merged_into") and x.get("merged_into") == x.get("claim_id"):
            x["merged_into"] = ""
            fixed.append(x["claim_id"])
    if fixed:
        res.setdefault("batch_defects", []).extend(
            f"merged_into self-reference cleared on {cid}" for cid in fixed
        )
        print(f"merged_into self-reference cleared: {len(fixed)} {fixed[:10]}")
    return fixed


def report(res: dict, repo: Path) -> None:
    j = res["judged"]
    items = res.get("items") or []
    no_def = [i["draft"]["id"] for i in items if i.get("defender") is None]
    no_pro = [i["draft"]["id"] for i in items if i.get("three_vote") and i.get("prosecutor") is None]
    print(f"items without defender vote: {len(no_def)} {no_def[:15]}")
    print(f"raising items without prosecutor vote: {len(no_pro)} {no_pro[:15]}")
    print(
        f"judged={len(j)} items={len(items)} batch_defects={len(res.get('batch_defects') or [])} "
        f"failed_components={res.get('failed_components')} failed_batches={len(res.get('failed_batches') or [])}"
    )
    print("routes:", collections.Counter(x["route"] for x in j))
    print("kinds:", collections.Counter(x["final_kind"] for x in j))
    print("status:", collections.Counter(x["final_status"] for x in j))
    merged = [x for x in j if x.get("merged_into")]
    print(f"merged_into set: {len(merged)}")

    # dedup: same path set + same check, different status (I.3.5 invariant)
    g = collections.defaultdict(list)
    for x in j:
        if x.get("merged_into"):
            continue
        g[(tuple(sorted(x["final_path"])), x["final_check"])].append(x)
    dups = {k: v for k, v in g.items() if len(v) > 1}
    print(f"same path+check groups: {len(dups)}")
    for k, v in dups.items():
        st = {x["final_status"] for x in v}
        flag = "STATUS-CONFLICT" if len(st) > 1 else "dup"
        print(f"  [{flag}] check={k[1][:70]} paths={list(k[0])[:2]} -> {[(x['claim_id'], x['final_status']) for x in v]}")

    ids = collections.Counter(x["claim_id"] for x in j)
    print("duplicate claim_ids:", [i for i, n in ids.items() if n > 1][:20])

    esc = [x for x in j if x["route"] == "ESCALATE"]
    print(f"ESCALATE={len(esc)}")
    for x in esc[:30]:
        print(f"  {x['claim_id']}: {x['reason'][:140]}")

    cf = [x for x in j if x["route"] == "CODE_FIX_CANDIDATE"]
    print(f"CODE_FIX_CANDIDATE={len(cf)}")
    for x in cf[:40]:
        print(f"  {x['claim_id']} [{x['final_status']}]: {(x.get('code_fix_brief') or '')[:140]}")

    can = [x["claim_id"] for x in j if x.get("canary_candidate")]
    print(f"canary candidates={len(can)}: {can[:10]}")

    check_inventory(j, repo)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Dump the I.2-I.3.5 workflow result to claims-judged.json and print the I.4 readiness report.",
    )
    parser.add_argument("--run", required=True, help="path to run.json")
    parser.add_argument("--journal", help="path to a workflow's journal.jsonl")
    parser.add_argument("--session-dir", help="Claude session directory (used with --workflow-id)")
    parser.add_argument("--workflow-id", help="workflow id under session-dir/subagents/workflows/")
    parser.add_argument(
        "--task-output",
        help="path to a Workflow task's <taskId>.output file (preferred: carries items with votes; "
        "a journal source is the fallback when it is missing or empty)",
    )
    args = parser.parse_args(argv)

    has_task = bool(args.task_output) and Path(args.task_output).is_file() and Path(args.task_output).stat().st_size > 0
    if not has_task and not args.journal and not (args.session_dir and args.workflow_id):
        parser.error("need a source: --task-output PATH (preferred), or --journal PATH, "
                     "or --session-dir DIR with --workflow-id ID")

    run = load_run(args.run)
    res = load_result(args)
    normalize_merged_into(res)

    out_path = run.research / "claims-judged.json"
    out_path.write_text(json.dumps(res, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"wrote {out_path}")

    report(res, run.repo)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
