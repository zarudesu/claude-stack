#!/usr/bin/env python3
"""Fold I.4 workflow results (probe/test/canary/classify) into claims-judged.json.

The I.4 workflow has no single aggregated task output: each agent's own
schema-shaped result (PROBE_SCHEMA/TEST_SCHEMA/CANARY_SCHEMA/CLASS_SCHEMA)
lands as one journal.jsonl line as it finishes. This stage reads those
lines, sorts each result into probes/tests/canaries/classify by which
fields it carries, and:

- always builds the report and flags below and prints them;
- unless --dry-run, writes run.research/i4-results.json (the report) and
  run.research/i4-brief-classes.json (the Classify phase's entries, one
  per code-fix brief -- see i4_classify_briefs.py);
- only with --write (and not --dry-run), folds probe/test/canary outcomes
  into run.research/claims-judged.json in place: a probe's exit codes and
  red-demo result are recorded on its claim; a test's final_check is
  trimmed to node ids that already existed or were just written (planned
  nodes that got dropped for budget reasons become debt); a verified
  canary sets claim.canary and claim.mutation.

Flags are printed for a human to read, never acted on automatically:
forbidden, probe_not_written, probe_not_green_at_head, probe_red_demo_failed,
probe_slow, probe_missing_result, test_file_red, test_not_written,
test_not_green, test_no_red_proof, unknown_claim, canary_unverified.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from gt_lib.journal import journal_path, read_journal  # noqa: E402
from gt_lib.paths import args_path, load_run, rel  # noqa: E402
from gt_lib.pytest_nodes import pytest_nodes_in_file  # noqa: E402


def load_results(jpath: Path) -> list[dict]:
    entries = read_journal(jpath)
    return [e["result"] for e in entries if isinstance(e.get("result"), dict)]


def load_head_override(path: str | None) -> dict:
    if not path:
        return {}
    out = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        parts = line.split("\t")
        if len(parts) >= 3:
            out[parts[1]] = (int(parts[0]), float(parts[2]))
    return out


def normalize_probe_file(pf: str, run) -> str:
    if not pf:
        return pf
    try:
        return rel(run, pf)
    except ValueError:
        return pf


def apply_head_override(probes: list[dict], override: dict) -> None:
    if not override:
        return
    for g in probes:
        for r in g.get("results") or []:
            base = (r.get("probe_file") or "").rsplit("/", 1)[-1]
            if base in override and override[base][0] != r.get("exit_at_head"):
                exit_code, runtime = override[base]
                r["_journal_exit_at_head"] = r.get("exit_at_head")
                r["exit_at_head"] = exit_code
                r["runtime_seconds"] = runtime
                print(f"head override: {base} journal exit {r['_journal_exit_at_head']} -> main run exit {exit_code}")


def apply_probes(probes: list[dict], J: dict, expected_probe_files: dict, write: bool, report: dict, flag) -> None:
    seen = set()
    for g in probes:
        if g.get("forbidden_action_taken"):
            flag("forbidden", g.get("group"), "probe agent reported forbidden_action_taken")
        for r in g.get("results") or []:
            cid = r["claim_id"]
            seen.add(r.get("probe_file"))
            entry = {
                "claim_id": cid, "probe_file": r.get("probe_file"), "written": r.get("written"),
                "exit_head": r.get("exit_at_head"), "red_exit": r.get("red_demo_exit"),
                "red_mut": (r.get("red_demo_mutation") or "")[:200], "runtime": r.get("runtime_seconds"),
                "left_out": r.get("left_out") or [], "notes": (r.get("notes") or "")[:300],
            }
            report["probes"].append(entry)
            if not r.get("written"):
                flag("probe_not_written", cid, (r.get("notes") or "")[:200])
                continue
            if r.get("exit_at_head") != 0:
                flag("probe_not_green_at_head", cid, f"exit {r.get('exit_at_head')}: {(r.get('head_line') or '')[:200]}")
            if r.get("red_demo_exit") != 1:
                flag("probe_red_demo_failed", cid, f"red exit {r.get('red_demo_exit')}: {(r.get('red_demo_line') or '')[:200]}")
            if r.get("runtime_seconds") and r["runtime_seconds"] > 20:
                flag("probe_slow", cid, f"{r['runtime_seconds']} s")
            if write and cid in J:
                for lo in r.get("left_out") or []:
                    J[cid].setdefault("debt", []).append({"description": f"no guard: {lo}", "severity": "normal", "evidence": r.get("probe_file")})
                J[cid]["_i4_probe"] = {"exit_head": r.get("exit_at_head"), "red_exit": r.get("red_demo_exit"), "red_mut": r.get("red_demo_mutation")}
    for pf, cid in expected_probe_files.items():
        if pf not in seen:
            flag("probe_missing_result", cid, pf)


def node_in_worktree(repo: Path, node: str) -> bool:
    """True when a pytest node id resolves in the worktree as it is now
    (after the I.4 test agents wrote their files): file::part must be
    defined in file; a bare file id needs the file to define any test."""
    if "::" in node:
        f, part = node.split("::", 1)
        return part in pytest_nodes_in_file(repo / f)
    return bool(pytest_nodes_in_file(repo / node))


def resolve_node(repo: Path, node: str | None, cid: str, flag) -> str | None:
    """The node id a test agent reports is a claim, not a fact: it wrote the
    test inside a class and reported the bare function id, or misspelled
    the name. Resolve it against the file as written: exact hit -> as is;
    exactly one node with the same test name (class-wrapped) -> that one,
    flagged node_resolved; otherwise flagged node_not_in_worktree and
    dropped, so a phantom id never reaches final_check."""
    if not node or "::" not in node:
        return node
    f, part = node.split("::", 1)
    nodes = pytest_nodes_in_file(repo / f)
    if part in nodes:
        return node
    tail = part.split("::")[-1]
    same = [n for n in nodes if n.split("::")[-1] == tail]
    if len(same) == 1:
        fixed = f"{f}::{same[0]}"
        flag("node_resolved", cid, f"{node} -> {fixed}")
        return fixed
    flag("node_not_in_worktree", cid, node)
    return None


def apply_tests(tests: list[dict], J: dict, existing: set, nodes_known: bool, write: bool, report: dict, flag, repo: Path) -> None:
    for t in tests:
        if t.get("forbidden_action_taken"):
            flag("forbidden", t.get("test_file"), "test agent reported forbidden_action_taken")
        if not t.get("file_green"):
            flag("test_file_red", t.get("test_file"), "file not green at HEAD")
        for r in t.get("results") or []:
            cid = r["claim_id"]
            entry = {
                "claim_id": cid, "test_file": t.get("test_file"), "node": r.get("node_id"), "written": r.get("written"),
                "green": r.get("green_at_head"), "red_file": r.get("red_demo_file"),
                "red_find": (r.get("red_demo_find") or "")[:120], "red_fail": (r.get("red_demo_failure") or "")[:200],
                "left_out": r.get("left_out") or [], "notes": (r.get("notes") or "")[:300],
            }
            report["tests"].append(entry)
            if not r.get("written"):
                flag("test_not_written", cid, (r.get("notes") or "")[:200])
                continue
            if not r.get("green_at_head"):
                flag("test_not_green", cid, r.get("node_id"))
            if not r.get("red_demo_failure"):
                flag("test_no_red_proof", cid, r.get("node_id"))
            if cid not in J:
                flag("unknown_claim", cid, t.get("test_file"))
                continue
            # resolved in dry-run too, so the report main reads shows it
            node = resolve_node(repo, r.get("node_id"), cid, flag)
            if write:
                cur = [n for n in (J[cid].get("final_check") or "").split(";") if n.strip()]
                if nodes_known:
                    keep = [n for n in cur if n in existing or any(e.startswith(n + "::") for e in existing)]
                else:
                    # Default: resolve every planned node id against the
                    # worktree as the test agents left it (AST scan, no
                    # pytest run). A planned id the agent wrote under a
                    # different id (class-wrapped, renamed) is a rename,
                    # not a budget drop.
                    keep = [n for n in cur if node_in_worktree(repo, n)]
                if node and node not in keep:
                    keep.append(node)
                renamed = [n for n in cur if n not in keep and node and n.split("::")[-1] == node.split("::")[-1]]
                dropped = [n for n in cur if n not in keep and n != node and n not in renamed]
                if renamed:
                    flag("node_renamed", cid, f"{renamed[0]} -> {node}")
                J[cid]["final_check"] = ";".join(keep)
                debts = J[cid].setdefault("debt", [])
                if dropped:
                    desc = "no guard: planned tests not written (test budget): " + ", ".join(n.split("::")[-1] for n in dropped)
                    if not any(d.get("description") == desc for d in debts):
                        debts.append({"description": desc, "severity": "normal", "evidence": ";".join(dropped)})
                for lo in r.get("left_out") or []:
                    desc = f"no guard: {lo}"
                    if not any(d.get("description") == desc for d in debts):
                        debts.append({"description": desc, "severity": "normal", "evidence": t.get("test_file")})
                J[cid]["_i4_test"] = {
                    "node": node, "red_file": r.get("red_demo_file"), "red_find": r.get("red_demo_find"),
                    "red_replace": r.get("red_demo_replace"), "red_fail": r.get("red_demo_failure"),
                }


def apply_canaries(canaries: list[dict], J: dict, write: bool, report: dict, flag) -> None:
    for c in canaries:
        cid = c["claim_id"]
        entry = {k: c.get(k) for k in ("claim_id", "mutation_file", "mutation_find", "mutation_replace", "red_nodes", "green_at_head", "suggestion_was_toothless")}
        entry["red_failure"] = (c.get("red_failure") or "")[:200]
        report["canaries"].append(entry)
        ok = bool(c.get("red_nodes")) and c.get("green_at_head") and c.get("mutation_find") and c.get("mutation_replace")
        if not ok:
            flag("canary_unverified", cid, json.dumps(entry)[:200])
            continue
        if write and cid in J:
            J[cid]["canary"] = True
            J[cid]["mutation"] = {"file": c["mutation_file"], "find": c["mutation_find"], "replace": c["mutation_replace"]}
            J[cid]["_i4_canary"] = {"red_nodes": c["red_nodes"], "red_failure": c.get("red_failure")}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Fold I.4 workflow results into claims-judged.json and write the I.4 report.",
    )
    parser.add_argument("--run", required=True, help="path to run.json")
    parser.add_argument("--journal", help="path to the I.4 workflow's journal.jsonl")
    parser.add_argument("--session-dir", help="Claude session directory (used with --workflow-id)")
    parser.add_argument("--workflow-id", help="workflow id under session-dir/subagents/workflows/")
    parser.add_argument("--nodes", help="file of existing pytest node ids, one per line (default: AST scan of the worktree)")
    parser.add_argument("--head-override", help="file of exit\\tbasename\\truntime lines from a fresh probe run")
    parser.add_argument("--write", action="store_true", help="fold results into claims-judged.json")
    parser.add_argument("--dry-run", action="store_true", help="print the report only, write nothing")
    args = parser.parse_args(argv)

    if not args.journal and not (args.session_dir and args.workflow_id):
        parser.error("need --journal, or --session-dir with --workflow-id")

    run = load_run(args.run)
    write = args.write and not args.dry_run
    nodes_known = args.nodes is not None

    jpath = Path(args.journal) if args.journal else journal_path(args.session_dir, args.workflow_id)
    results = load_results(jpath)

    classify = [r for r in results if isinstance(r.get("entries"), list)]
    probes = [r for r in results if "group" in r and "results" in r]
    tests = [r for r in results if "test_file" in r]
    canaries = [r for r in results if "mutation_file" in r]
    print(f"journal results: {len(results)} = classify {len(classify)} probes {len(probes)} tests {len(tests)} canaries {len(canaries)}")

    for g in probes:
        for r in g.get("results") or []:
            r["probe_file"] = normalize_probe_file(r.get("probe_file") or "", run)
    apply_head_override(probes, load_head_override(args.head_override))

    judged_path = run.research / "claims-judged.json"
    judged = json.loads(judged_path.read_text(encoding="utf-8"))
    J = {j["claim_id"]: j for j in judged["judged"]}

    i4_args_path = args_path(run, "i4")
    if not i4_args_path.exists():
        parser.error(f"{i4_args_path} not found -- run i4_build_args first")
    i4_args = json.loads(i4_args_path.read_text(encoding="utf-8"))
    expected_probe_files = {p["probe_file"]: p["claim_id"] for g in i4_args["probe_groups"] for p in g["probes"]}
    existing = set()
    if args.nodes:
        existing = {line.strip() for line in Path(args.nodes).read_text(encoding="utf-8").splitlines() if line.strip()}

    report = {"probes": [], "tests": [], "canaries": [], "flags": []}
    flag = lambda kind, cid, msg: report["flags"].append({"kind": kind, "claim_id": cid, "msg": msg})  # noqa: E731

    apply_probes(probes, J, expected_probe_files, write, report, flag)
    apply_tests(tests, J, existing, nodes_known, write, report, flag, run.repo)
    apply_canaries(canaries, J, write, report, flag)

    class_entries = classify[0]["entries"] if classify else []
    if not classify:
        print("warning: no Classify result found in the journal", file=sys.stderr)

    if not args.dry_run:
        (run.research / "i4-results.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        (run.research / "i4-brief-classes.json").write_text(json.dumps(class_entries, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"wrote {run.research}/i4-results.json and i4-brief-classes.json ({len(class_entries)} entries)")
    else:
        print("dry run: nothing written")

    print("flags:", dict(Counter(f["kind"] for f in report["flags"])))
    for f in report["flags"]:
        print(f"  {f['kind']:26s} {f['claim_id']}: {f['msg'][:160]}")
    print(
        f"probes ok: {sum(1 for p in report['probes'] if p['written'] and p['exit_head'] == 0 and p['red_exit'] == 1)}/{len(report['probes'])}; "
        f"tests ok: {sum(1 for t in report['tests'] if t['written'] and t['green'] and t['red_fail'])}/{len(report['tests'])}; "
        f"canaries ok: {sum(1 for c in report['canaries'] if c['red_nodes'] and c['green_at_head'])}/{len(report['canaries'])}"
    )

    if write:
        judged_path.write_text(json.dumps(judged, indent=2, ensure_ascii=False), encoding="utf-8")
        print("claims-judged.json updated")
        reassemble_status(run, judged)

    return 0


def reassemble_status(run, judged: dict) -> None:
    """Rebuild claims/debt of the repo's STATUS.yaml from the folded
    claims-judged.json (same assembler as I.3.5); meta and edges are kept
    as they are in the file. Without this the repo copy would still carry
    the draft's planned node ids and no canary."""
    import yaml
    from stages.i35_assemble_status import build, validate

    status_path = run.repo / "STATUS.yaml"
    if not status_path.exists():
        print(f"note: {status_path} not present, STATUS.yaml not reassembled", file=sys.stderr)
        return
    doc = yaml.safe_load(status_path.read_text(encoding="utf-8")) or {}
    meta = doc.get("meta") or {}
    defects: list = []
    claims, debt, _merged = build(judged["judged"], judged.get("items") or [], run.repo, meta.get("last_audited") or run.date, defects)
    doc["claims"], doc["debt"] = claims, debt
    doc.setdefault("edges", [])
    status_path.write_text(yaml.dump(doc, Dumper=yaml.SafeDumper, sort_keys=False, allow_unicode=True, width=1000), encoding="utf-8")
    print(f"reassembled {status_path} claims={len(claims)} debt={len(debt)} canary={sum(1 for c in claims if c.get('canary'))} defects={len(defects)}")
    for d in defects:
        print(f"  defect {d}")
    validate(run.repo, run.skill / "templates")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
