#!/usr/bin/env python3
"""Versioned project knowledge: routes, freshness, task packets and explicit review receipts.

No model calls, test execution, source edits or automatic semantic approval.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import yaml

MODEL = ".ground-truth/model.yaml"
RECEIPTS = ".ground-truth/review.json"
MANAGED = {MODEL, RECEIPTS, RECEIPTS + ".lock", ".ground-truth/workspace"}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def git(root, *args):
    proc = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=30)
    if proc.returncode:
        raise ValueError(f"git {args[0]} failed in {root}")
    return proc.stdout


def discover(start):
    for root in (start.resolve(), *start.resolve().parents):
        if (root / MODEL).is_file():
            return root
        pointer = root / ".ground-truth/workspace"
        if pointer.is_file():
            target = (root / pointer.read_text().strip()).resolve()
            if not (target / MODEL).is_file():
                raise ValueError(f"workspace pointer has no model: {target}")
            return target
    return None


def relative(value):
    if not isinstance(value, str) or not value or Path(value).is_absolute() or ".." in Path(value).parts:
        raise ValueError(f"expected a non-empty repo-relative path/pattern: {value!r}")
    return value.removeprefix("./").rstrip("/") or "."


def bind_checkout(view, start):
    """Never serve canonical-checkout receipts for another worktree of that repo."""
    try:
        active = Path(git(start, "rev-parse", "--show-toplevel").strip()).resolve()
        common = git(active, "rev-parse", "--path-format=absolute", "--git-common-dir").strip()
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return
    for rid, location in view["repositories"].items():
        repo = Path(location)
        if rid not in view["repository_errors"] and repo != active:
            if git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir").strip() == common:
                raise ValueError(f"active worktree {active} differs from registered {repo}; use a task-local workspace model")


def matches(path, pattern):
    pattern = relative(pattern)
    return pattern in (".", "**", "*") or path == pattern or path.startswith(pattern + "/") or fnmatch.fnmatchcase(path, pattern)


def local_file(root, name):
    path = root / relative(name)
    # No symlink escapes or silently imported outside documentation.
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"source escapes repository: {name}")
    return path


def file_digest(root, name):
    path = local_file(root, name)
    if not path.is_file():
        raise ValueError(f"missing source: {name}")
    data = path.read_bytes()
    return digest([hashlib.sha256(data).hexdigest(), bool(path.stat().st_mode & 0o111)])


def load(root):
    model = yaml.safe_load((root / MODEL).read_text())
    if not isinstance(model, dict) or model.get("version") != 1:
        raise ValueError("model.version must be 1")
    repos, areas = model.get("repositories"), model.get("areas")
    if not isinstance(repos, dict) or not repos or not isinstance(areas, list) or not areas:
        raise ValueError("model needs non-empty repositories and areas")
    allowed = {"version", "repositories", "areas", "enforcement"}
    if set(model) - allowed or model.get("enforcement", "advisory") not in ("advisory", "blocking"):
        raise ValueError("unknown model fields or enforcement")
    nodes = {}
    for node in areas:
        if not isinstance(node, dict) or set(node) - {
            "id", "repo", "summary", "sources", "docs", "depends_on", "entrypoints", "checks", "unknowns", "valid_until"
        }:
            raise ValueError("unknown area fields")
        cid = node.get("id")
        if not isinstance(cid, str) or not cid or cid in nodes or node.get("repo") not in repos:
            raise ValueError(f"invalid/duplicate area id or repository: {cid}")
        for key in ("sources", "docs", "depends_on", "entrypoints", "checks", "unknowns"):
            values = node.get(key, [])
            if not isinstance(values, list) or any(not isinstance(v, str) or not v for v in values):
                raise ValueError(f"{cid}.{key} must be a list of non-empty strings")
        if not node.get("sources") or not node.get("docs") or not isinstance(node.get("summary"), str):
            raise ValueError(f"{cid} requires sources, canonical docs and summary")
        for path in node["sources"] + node["docs"]:
            relative(path)
        if "valid_until" in node:
            if not isinstance(node["valid_until"], str):
                raise ValueError(f"{cid}.valid_until must be a quoted ISO timestamp")
            if datetime.fromisoformat(node["valid_until"].replace("Z", "+00:00")).tzinfo is None:
                raise ValueError(f"{cid}.valid_until requires a timezone")
        nodes[cid] = node
    for node in areas:
        missing = set(node.get("depends_on", [])) - set(nodes)
        if missing:
            raise ValueError(f"{node['id']}: unknown dependencies {sorted(missing)}")
    for rid, repo in repos.items():
        if not isinstance(repo, dict) or set(repo) - {"path", "watch", "exclude"} or not isinstance(repo.get("path"), str):
            raise ValueError(f"{rid}: invalid repository declaration")
        if not repo["path"] or Path(repo["path"]).is_absolute():
            raise ValueError(f"{rid}: use a relocatable workspace-relative repository path")
        watch = repo.get("watch", ["."])
        if not isinstance(watch, list) or not watch:
            raise ValueError(f"{rid}: watch must be a non-empty list")
        for pattern in watch:
            relative(pattern)
        exclusions = repo.get("exclude", [])
        if not isinstance(exclusions, list):
            raise ValueError(f"{rid}: exclude must be a list")
        for item in exclusions:
            if not isinstance(item, dict) or set(item) != {"path", "why"} or not item["why"]:
                raise ValueError(f"{rid}: every exclusion needs path and why")
            relative(item["path"])
    state_path = root / RECEIPTS
    state = json.loads(state_path.read_text()) if state_path.exists() else {"version": 1, "areas": {}}
    if not isinstance(state, dict) or state.get("version") != 1 or not isinstance(state.get("areas"), dict):
        raise ValueError("invalid review receipts")
    return model, nodes, state


def closure(seeds, nodes, reverse=False):
    seen, todo = set(), list(seeds)
    while todo:
        cid = todo.pop()
        if cid in seen:
            continue
        seen.add(cid)
        todo.extend([n for n, v in nodes.items() if cid in v.get("depends_on", [])]
                    if reverse else nodes[cid].get("depends_on", []))
    return seen


def inspect(root):
    model, nodes, state = load(root)
    files, locations, errors, gaps = {}, {}, {}, {}
    common_dirs = {}
    for rid, spec in model["repositories"].items():
        repo = (root / spec["path"]).resolve()
        locations[rid] = str(repo)
        try:
            if Path(git(repo, "rev-parse", "--show-toplevel").strip()).resolve() != repo:
                raise ValueError("path is not a repository root")
            common = git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir").strip()
            if common in common_dirs:
                raise ValueError(f"duplicate worktree identity of {common_dirs[common]}")
            common_dirs[common] = rid
            names = set(git(repo, "ls-files", "--cached", "--others", "--exclude-standard", "-z").split("\0")) - {""}
            # Explicit file sources can opt in safe ignored local configuration.
            # Directory/glob sources still never sweep ignored caches or secrets.
            for node in nodes.values():
                if node["repo"] == rid:
                    for source in node["sources"]:
                        name = relative(source)
                        if not any(c in name for c in "*?[") and local_file(repo, name).is_file():
                            names.add(name)
            # gitlinks/nested repositories must have their own registry entry.
            names = {n for n in names if not (repo / n).is_dir() and n not in MANAGED}
            watched = {n for n in names if any(matches(n, p) for p in spec.get("watch", ["."]))}
            watched = {n for n in watched if not any(matches(n, e["path"]) for e in spec.get("exclude", []))}
            files[rid] = watched
            owned = [n for n in nodes.values() if n["repo"] == rid]
            gaps[rid] = sorted(n for n in watched if not any(
                any(matches(n, p) for p in area["sources"]) or n in area["docs"] for area in owned))
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            errors[rid], files[rid], gaps[rid] = str(exc), set(), []
    own, inputs, local_errors = {}, {}, {}
    for cid, node in nodes.items():
        rid, selected = node["repo"], {}
        problems = [errors[rid]] if rid in errors else []
        repo = Path(locations[rid])
        for pattern in node["sources"]:
            matched = [n for n in files[rid] if matches(n, pattern)]
            if not matched:
                problems.append(f"source pattern matches no watched files: {pattern}")
            for name in matched:
                try:
                    selected[name] = file_digest(repo, name)
                except (OSError, ValueError) as exc:
                    problems.append(str(exc))
        for name in node["docs"]:
            try:
                selected[name] = file_digest(repo, name)
            except (OSError, ValueError) as exc:
                problems.append(str(exc))
        if node.get("valid_until"):
            deadline = datetime.fromisoformat(node["valid_until"].replace("Z", "+00:00"))
            if deadline.tzinfo is None or datetime.now(timezone.utc) >= deadline:
                problems.append("dated external evidence expired or lacks timezone")
        inputs[cid], local_errors[cid] = selected, problems
        # A checkout relocation is not a semantic edit. Reuse reviewed knowledge
        # only if all declared content, scope and dependency definitions match.
        scope = {k: v for k, v in model["repositories"][rid].items() if k != "path"}
        own[cid] = digest({"node": node, "repository_scope": scope, "inputs": selected})
    stamps = {cid: digest({d: own[d] for d in sorted(closure([cid], nodes))}) for cid in nodes}
    areas = {}
    for cid, node in nodes.items():
        previous = state["areas"].get(cid, {})
        if not isinstance(previous, dict):
            raise ValueError(f"invalid review receipt for {cid}")
        upstream = closure([cid], nodes)
        problems = [f"{d}: {p}" for d in sorted(upstream) for p in local_errors[d]]
        affected_gaps = {nodes[d]["repo"] for d in upstream}
        problems += [f"unclassified files in {rid}" for rid in sorted(affected_gaps) if gaps[rid]]
        old_inputs = previous.get("inputs", {})
        if not isinstance(old_inputs, dict):
            raise ValueError(f"invalid input fingerprints for {cid}")
        changed = sorted(n for n in set(old_inputs) | set(inputs[cid]) if old_inputs.get(n) != inputs[cid].get(n))
        fresh = previous.get("stamp") == stamps[cid] and not problems
        areas[cid] = {"fresh": fresh, "stamp": stamps[cid], "changed_files": changed,
                      "errors": problems, "inputs": inputs[cid],
                      "reason": "current" if fresh else ("unreviewed" if not previous else "source/docs/model/dependency drift")}
    removed = sorted(set(state["areas"]) - set(nodes))
    revision = digest({"model": model, "stamps": stamps, "errors": errors,
                       "gaps": gaps, "area_errors": local_errors, "removed": removed})
    return {"root": str(root), "revision": revision, "model": model, "nodes": nodes,
            "areas": areas, "repositories": locations, "gaps": gaps, "repository_errors": errors,
            "removed_areas": removed, "state": state}


def summary(view):
    stale = [cid for cid, area in view["areas"].items() if not area["fresh"]]
    return {"revision": view["revision"], "ready": not stale and not view["removed_areas"]
            and not view["repository_errors"] and not any(view["gaps"].values()),
            "stale": stale, "removed_areas": view["removed_areas"], "gaps": view["gaps"],
            "repository_errors": view["repository_errors"]}


def packet(view, selected, max_chars):
    nodes = view["nodes"]
    if not selected or set(selected) - set(nodes):
        raise ValueError("select known --area id(s); use list for routes")
    required = closure(selected, nodes)
    impacted = closure(selected, nodes, reverse=True) - set(selected)
    # Shared tests/config/docs can have several owners without dependency edges.
    # Route those owners without loading another set of document bodies.
    required_inputs = {}
    for cid in required:
        required_inputs.setdefault(nodes[cid]["repo"], set()).update(view["areas"][cid]["inputs"])
    co_owned = {cid for cid, node in nodes.items() if cid not in required and
                set(view["areas"][cid]["inputs"]) & required_inputs.get(node["repo"], set())}
    stale = sorted(n for n in required if not view["areas"][n]["fresh"])
    out = {"revision": view["revision"], "selected": selected,
           "required_areas": sorted(required), "affected_consumers": sorted(impacted),
           "co_owned_areas": sorted(co_owned),
           "fresh": not stale and not view["removed_areas"], "stale": stale,
           "routes": [{"id": n, **{k: nodes[n].get(k, []) for k in ("repo", "summary", "entrypoints", "checks", "unknowns")},
                       "repository_path": view["repositories"][nodes[n]["repo"]],
                       "fresh": view["areas"][n]["fresh"]}
                      for n in sorted(required | impacted | co_owned)],
           "documents": [], "omitted_documents": [], "packet_complete": True}
    used = len(json.dumps(out, ensure_ascii=False))
    if out["fresh"]:
        seen = set()
        for cid in sorted(required):
            node = nodes[cid]
            for name in node["docs"]:
                key = (node["repo"], name)
                if key in seen:
                    continue
                seen.add(key)
                path = local_file(Path(view["repositories"][key[0]]), name)
                raw = path.read_bytes()
                if digest([hashlib.sha256(raw).hexdigest(), bool(path.stat().st_mode & 0o111)]) != view["areas"][cid]["inputs"][name]:
                    raise ValueError("canonical document changed while building context")
                body = raw.decode("utf-8")
                entry = {"repo": key[0], "path": name, "text": body}
                size = len(json.dumps(entry, ensure_ascii=False))
                if used + size > max_chars:
                    out["omitted_documents"].append({"repo": key[0], "path": name})
                    out["packet_complete"] = False
                else:
                    used += size
                    out["documents"].append(entry)
    else:
        out["packet_complete"] = False
        out["repair"] = {n: {k: view["areas"][n][k] for k in ("reason", "changed_files", "errors")} for n in stale}
    if used > max_chars:
        out["packet_complete"] = False
    return out


def refresh(root, selected, expected, evidence, prune=False):
    """Explicit semantic attestation, bound to reviewed input content; never called by a hook."""
    lock = root / (RECEIPTS + ".lock")
    fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        view = inspect(root)
        if view["revision"] != expected:
            raise ValueError("inputs changed after review; get a fresh context/status revision")
        if not selected or set(selected) - set(view["nodes"]):
            raise ValueError("select known areas to refresh")
        if any(view["areas"][cid]["errors"] for cid in selected):
            raise ValueError("cannot refresh missing sources, expired evidence or unclassified files")
        state = view["state"]
        for cid in selected:
            area = view["areas"][cid]
            state["areas"][cid] = {"stamp": area["stamp"], "inputs": area["inputs"],
                "reviewed_at": datetime.now(timezone.utc).isoformat(), "evidence": evidence}
        if prune:
            for cid in view["removed_areas"]:
                del state["areas"][cid]
        # Detect changes during review/write preparation as well as before it.
        if inspect(root)["revision"] != expected:
            raise ValueError("inputs changed while preparing review receipt")
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=root / ".ground-truth",
                                         prefix=".review-", delete=False) as handle:
            tmp = Path(handle.name)
            try:
                json.dump(state, handle, indent=2, ensure_ascii=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
                os.replace(tmp, root / RECEIPTS)
            finally:
                if tmp.exists():
                    tmp.unlink()
    finally:
        os.close(fd)
        lock.unlink()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list")
    status = commands.add_parser("status")
    status.add_argument("--brief", action="store_true")
    commands.add_parser("check")
    hook = commands.add_parser("hook")
    hook.add_argument("--event", choices=["start", "stop"], required=True)
    context = commands.add_parser("context")
    context.add_argument("--area", action="append", required=True)
    context.add_argument("--max-chars", type=int, default=24000)
    review = commands.add_parser("refresh")
    selection = review.add_mutually_exclusive_group(required=True)
    selection.add_argument("--area", action="append")
    selection.add_argument("--all", action="store_true")
    review.add_argument("--reviewed", action="store_true", required=True)
    review.add_argument("--expect", required=True)
    review.add_argument("--evidence", required=True)
    review.add_argument("--prune-removed", action="store_true")
    args = parser.parse_args(argv)
    try:
        root = args.root.resolve() if args.root else discover(Path.cwd())
        if root is None:
            raise ValueError("no prepared model; run ground-truth init (STATUS alone is not project memory)")
        view = inspect(root)
        bind_checkout(view, Path.cwd())
        if args.command == "list":
            print(json.dumps([{"id": cid, "repo": n["repo"], "summary": n["summary"],
                               "fresh": view["areas"][cid]["fresh"]} for cid, n in view["nodes"].items()], ensure_ascii=False))
            return 0
        if args.command == "refresh":
            if not args.evidence.strip():
                raise ValueError("review evidence must describe actual checks/semantic review")
            refresh(root, list(view["nodes"]) if args.all else args.area,
                    args.expect, args.evidence, args.prune_removed)
            print("review receipts updated; unrelated areas were not approved")
            return 0
        if args.command == "context":
            out = packet(view, args.area, args.max_chars)
            latest = inspect(root)
            if latest["revision"] != view["revision"]:
                out.update(fresh=False, packet_complete=False, documents=[],
                           error="inputs changed while building context; request a fresh packet")
            print(json.dumps(out, ensure_ascii=False, indent=1))
            return 0 if out["fresh"] and out["packet_complete"] else 1
        out = summary(view)
        if args.command == "hook":
            if args.event == "start" or not out["ready"]:
                print(f"ground-truth memory: ready={out['ready']}; {len(view['areas'])} areas; "
                      f"stale={','.join(out['stale'][:8]) or '-'}; "
                      f"unclassified={sum(map(len, out['gaps'].values()))}; "
                      "use gt_context.py list then context --area ID. Never auto-refresh receipts.",
                      file=sys.stderr if args.event == "stop" else sys.stdout)
            return 2 if args.event == "stop" and not out["ready"] and view["model"].get("enforcement") == "blocking" else 0
        if args.command == "status" and args.brief:
            print(f"ground-truth memory: {len(view['areas'])} areas, {len(out['stale'])} stale, "
                  f"{sum(map(len, out['gaps'].values()))} unclassified files; "
                  "use gt_context.py list, then context --area ID before assigning work")
            if out["stale"]:
                print("needs review: " + ", ".join(out["stale"][:8]))
        else:
            print(json.dumps(out, ensure_ascii=False, indent=1))
        return 0 if out["ready"] else 1
    except (OSError, ValueError, TypeError, KeyError, yaml.YAMLError, subprocess.TimeoutExpired) as exc:
        print(f"ground-truth memory: NOT VERIFIED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
