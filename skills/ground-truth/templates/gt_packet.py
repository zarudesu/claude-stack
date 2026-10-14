#!/usr/bin/env python3
"""Compact context: pointers to the claims behind a path or an area, checked before they are shown.

Usage: python3 tools/ground_truth/gt.py context (--path P... | --area A) [--limit 15] [--all] [--json]
       python3 tools/ground_truth/gt.py show <claim-id>|area:<id>|<path>

Claims rank by 4*exact path + 3*path prefix + 2*canary + 1*implemented, then
id, so one checkout prints the same bytes every time. Before a claim is
shown its paths must exist, its quoted file:line citations must hold and
its area's review receipt (model.yaml), if it has one, must be fresh. A
claim failing any of these is listed under "drifted, do not trust", never
as a fact. A path whose bytes differ from the last green verify --mode=sync
is marked "changed since last green verify". Document bodies are not
printed, only their paths. The only write is the local usage record
(gt_usage.py) of the claims shown as facts, or printed by gt show.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

import contract_lib  # noqa: E402
import gt_pending  # noqa: E402
import gt_store  # noqa: E402
import gt_usage  # noqa: E402

try:
    import gt_context  # noqa: E402
except ImportError:
    gt_context = None  # installed only with project memory

DIGESTS = "ground-truth-digests.json"
MAX_DRIFTED = 5
SUMMARY_CHARS = 120


def _git(root: Path, *args: str) -> str:
    cp = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
    return cp.stdout.strip() if cp.returncode == 0 else ""


def digests(root: Path, claims: list) -> dict:
    out = {}
    for claim in claims:
        for rel in contract_lib._claim_path_targets(claim) if isinstance(claim, dict) else []:
            if (root / rel).is_file():
                out[rel] = hashlib.sha256((root / rel).read_bytes()).hexdigest()
    return out


def write_digests(root: Path) -> None:
    """sha256 of every claim path, kept in the git dir; verify.py --mode=sync writes it when green."""
    git_dir = _git(root, "rev-parse", "--absolute-git-dir")
    if git_dir:
        claims = gt_store.load(root).data.get("claims") or []
        Path(git_dir, DIGESTS).write_text(json.dumps(digests(root, claims), sort_keys=True))


def _covers(a: str, b: str) -> bool:
    return a.startswith(b + "/") or b.startswith(a + "/")


def _nodes(root: Path) -> tuple[Path | None, dict]:
    """(model root, area id -> model.yaml node); (None, {}) without project memory."""
    model_root = gt_context.discover(root) if gt_context else None
    return (model_root, gt_context.load(model_root)[1]) if model_root else (None, {})


def _claims(root: Path, contract) -> dict:
    """claim id -> (claim, area, paths); a STATUS.yaml claim's area comes from its first path."""
    model_areas = gt_pending.model_areas(root)
    claims = {}
    for claim in contract.data.get("claims") or []:
        if isinstance(claim, dict) and isinstance(claim.get("id"), str):
            own = contract_lib._claim_path_targets(claim)
            where = contract.area_of_claim.get(claim["id"]) or (
                gt_pending.area_of(own[0], model_areas) if own else "global")
            claims.setdefault(claim["id"], (claim, where, own))
    return claims


def _drift(root: Path, claim: dict, paths: list[str], receipt: str | None) -> str | None:
    for rel in paths:
        if not (root / rel).exists():
            return f"{rel} missing"
    problems = contract_lib.cited_line_problems(root, claim)
    if problems:
        return problems[0]
    return "area receipt stale" if receipt == "stale" else None


def build(root: Path, paths: list[str] | None, area: str | None, limit: int, everything: bool) -> dict:
    contract = gt_store.load(root)
    data = contract.data
    claims = _claims(root, contract)
    paths = paths or []

    def on_path(cid: str) -> int:
        own = claims[cid][2]
        return 4 * any(p in own for p in paths) + 3 * any(_covers(p, q) for p in paths for q in own)

    def score(cid: str) -> int:
        claim = claims[cid][0]
        return on_path(cid) + 2 * bool(claim.get("canary")) + (claim.get("status") == "implemented")

    if area is not None:
        selected = {area}
    else:
        model_areas = gt_pending.model_areas(root)
        selected = {gt_pending.area_of(p, model_areas) for p in paths}
        selected |= {w for cid, (_, w, _) in claims.items() if on_path(cid)}
    model_root, nodes = _nodes(root)
    if area is not None and area not in nodes and not any(w == area for _, w, _ in claims.values()):
        raise ValueError(f"unknown area {area!r}")
    ids = sorted(selected & set(nodes))
    view = gt_context.inspect(model_root, areas=ids) if ids else {"areas": {}}
    receipts = {a: "fresh" if view["areas"][a]["fresh"] else "stale" for a in ids}
    ranked = sorted((cid for cid, (_, w, _) in claims.items() if w in selected), key=lambda c: (-score(c), c))
    drifted, facts = [], []
    for cid in ranked:
        claim, where, own = claims[cid]
        reason = _drift(root, claim, own, receipts.get(where))
        (drifted if reason else facts).append((cid, reason))
    if not everything:
        drifted = drifted[:min(MAX_DRIFTED, limit)]
        facts = facts[:limit - len(drifted)]
    git_dir = _git(root, "rev-parse", "--absolute-git-dir")
    last = Path(git_dir, DIGESTS) if git_dir else None
    last = json.loads(last.read_text()) if last and last.is_file() else {}
    now = digests(root, [claims[cid][0] for cid, _ in facts]) if last else {}
    shown = {cid for cid, _ in drifted + facts}
    try:
        pending = gt_pending.load(root)[2].values()
    except gt_pending.Unreadable:
        pending = []
    out = {"rev": _git(root, "rev-parse", "--short", "HEAD") or "-", "layout": gt_store.detect_layout(root),
           "query": {"area": area} if area is not None else {"paths": paths},
           "areas": [], "drifted": [], "more": []}
    order = [claims[cid][1] for cid in ranked if cid in shown]
    for aid in sorted(selected, key=lambda a: (order.index(a) if a in order else len(order), a)):
        mine = [cid for cid in ranked if claims[cid][1] == aid]
        hidden = len([cid for cid in mine if cid not in shown])
        if hidden:
            out["more"].append({"area": aid, "count": hidden, "command": f"gt context --area {aid} --all"})
        if aid not in order:
            continue
        node = nodes.get(aid, {})
        summary = " ".join(str(node.get("summary", "")).split())
        out["areas"].append({
            "id": aid, "receipt": receipts.get(aid, "none"), "claims": len(mine),
            "shown": len([cid for cid in mine if cid in shown]),
            "debt_open": len([d for d in data.get("debt") or [] if isinstance(d, dict)
                              and d.get("state") == "open" and d.get("ref") in mine]),
            "pending": len([e for e in pending if e.get("area") == aid]),
            "summary": summary if len(summary) <= SUMMARY_CHARS else summary[:SUMMARY_CHARS - 1] + "…",
            "docs": node.get("docs", []),
            "consumers": sorted(n for n, v in nodes.items() if aid in v.get("depends_on", [])),
            "facts": [{"id": cid, "status": claims[cid][0].get("status") or claims[cid][0].get("kind", "-"),
                       "canary": bool(claims[cid][0].get("canary")), "paths": claims[cid][2],
                       "check": claims[cid][0].get("check"),
                       "changed": any(last.get(p) != now[p] for p in claims[cid][2] if p in now)}
                      for cid, _ in facts if claims[cid][1] == aid],
        })
    out["drifted"] = [{"id": cid, "area": claims[cid][1], "reason": reason} for cid, reason in drifted]
    return out


def render(packet: dict) -> str:
    query = packet["query"]
    lines = [f"gt context @{packet['rev']} {packet['layout']}  "
             + (f"area: {query['area']}" if "area" in query else "paths: " + " ".join(query["paths"]))]
    for area in packet["areas"]:
        receipt = {"fresh": "receipt fresh", "stale": "receipt stale"}.get(area["receipt"], "no receipt")
        lines.append(f"area {area['id']} [{receipt}] claims {area['claims']} shown {area['shown']} | "
                     f"debt open {area['debt_open']} | pending {area['pending']}")
        for key in ("summary", "docs", "consumers"):
            value = ", ".join(area[key]) if isinstance(area[key], list) else area[key]
            if value:
                lines.append(f"  {key}: {value}")
        for fact in area["facts"]:
            lines.append(f"  {fact['id']} {fact['status']}" + (" canary" if fact["canary"] else "")
                         + (f" {','.join(fact['paths'])}" if fact["paths"] else "")
                         + (f" check {fact['check']}" if fact["check"] else "")
                         + (" (changed since last green verify)" if fact["changed"] else ""))
    if packet["drifted"]:
        lines.append("drifted, do not trust:")
        lines += [f"  {d['id']}  {d['reason']}" for d in packet["drifted"]]
    lines += [f"+{m['count']} more: {m['command']}" for m in packet["more"]]
    return "\n".join(lines) + "\n"


def _root(arg: Path | None) -> Path:
    return (arg or gt_store.find_root(Path(__file__).resolve().parent) or Path.cwd()).resolve()


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="gt context", description=__doc__.splitlines()[0])
    query = parser.add_mutually_exclusive_group(required=True)
    query.add_argument("--path", nargs="+")
    query.add_argument("--area")
    parser.add_argument("--limit", type=int, default=15)
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--root", type=Path)
    args = parser.parse_args(argv)
    try:
        packet = build(_root(args.root), args.path and [p.removeprefix("./").rstrip("/") for p in args.path],
                       args.area, args.limit, args.all)
        gt_usage.touch(_root(args.root), [fact["id"] for area in packet["areas"] for fact in area["facts"]])
    except (gt_store.ContractError, ValueError, OSError, yaml.YAMLError) as exc:
        print(f"gt context: {exc}", file=sys.stderr)
        return 3
    print(json.dumps(packet, ensure_ascii=False, indent=1) if args.json else render(packet), end="\n" if args.json else "")
    return 0


def show_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="gt show", description="print one claim, area or the claims of a path")
    parser.add_argument("target", help="<claim-id>, area:<id> or a repo-relative path")
    parser.add_argument("--root", type=Path)
    args = parser.parse_args(argv)
    root = _root(args.root)
    try:
        contract = gt_store.load(root)
        if args.target.startswith("area:"):
            aid = args.target[len("area:"):]
            nodes = _nodes(root)[1]
            ids = sorted(cid for cid, (_, w, _) in _claims(root, contract).items() if w == aid)
            sources = dict(gt_store.area_index(root)).get(aid)
            if not (ids or sources or aid in nodes):
                raise ValueError(f"unknown area {aid!r}")
            print(yaml.safe_dump({"area": aid, "sources": sources, "model": nodes.get(aid), "claims": ids},
                                 sort_keys=False, allow_unicode=True), end="")
            return 0
        target = args.target.removeprefix("./").rstrip("/")
        ids = [args.target] if ("claim", args.target) in contract.origin else [
            c["id"] for c in contract.data.get("claims") or [] if isinstance(c, dict) and isinstance(c.get("id"), str)
            and any(p == target or _covers(target, p) for p in contract_lib._claim_path_targets(c))]
        if not ids:
            raise ValueError(f"no claim id or claim path matches {args.target!r}")
        gt_usage.touch(root, ids)
        for cid in ids:
            rel, first, last = contract.origin[("claim", cid)]
            print(f"# {rel}:{first}-{last}")
            print("\n".join((root / rel).read_text().splitlines()[first - 1:last]))
    except (gt_store.ContractError, ValueError, OSError, yaml.YAMLError) as exc:
        print(f"gt show: {exc}", file=sys.stderr)
        return 3 if isinstance(exc, gt_store.ContractError) else 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
