#!/usr/bin/env python3
"""Blast radius for a claim id or a path: direct importers plus declared
STATUS.yaml relationships. Computed fresh on every call -- nothing here is
cached to disk (a cached graph would go stale the moment it's read, the
same failure mode STATUS.yaml itself refuses for live_state claims).

Usage:
    python3 tools/ground_truth/blast_radius.py <path-or-claim-id> [--root DIR]

Exit 0 in all normal cases (including "orphan path, no claim covers it" --
that is itself a useful answer). Exit 1 only when the argument is neither
an existing path under root nor a known claim id.
"""
from __future__ import annotations

import argparse
import ast
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    import gt_hook_state
except ImportError:
    gt_hook_state = None

PY_SUFFIX = ".py"
GO_SUFFIX = ".go"
JS_SUFFIXES = (".ts", ".tsx", ".js", ".jsx")


def find_root(start: Path) -> Path:
    """Walk up from `start` looking for a directory containing STATUS.yaml.
    Falls back to the current working directory if none is found."""
    cur = start.resolve()
    for candidate in [cur, *cur.parents]:
        if (candidate / "STATUS.yaml").is_file():
            return candidate
    return Path.cwd()


def load_status(root: Path) -> dict:
    status_file = root / "STATUS.yaml"
    if not status_file.is_file() or yaml is None:
        return {"claims": [], "edges": []}
    try:
        data = yaml.safe_load(status_file.read_text()) or {}
    except Exception:
        data = {}
    # get(...) or [] normalizes an explicit `claims: null`/`edges: null` the
    # same as an absent key -- setdefault leaves an explicit null in place
    # and the caller crashes iterating over it.
    data["claims"] = data.get("claims") or []
    data["edges"] = data.get("edges") or []
    return data


def claim_paths(claim: dict) -> list[str]:
    p = claim.get("path")
    if p is None:
        return []
    if isinstance(p, list):
        return list(p)
    return [p]


def _ack_rels(root: Path, claim: dict | None, target_path: Path | None) -> list[str]:
    """The repo-relative paths this run reported on: every path of a
    claim (existing or not -- the ack covers the whole claim), or the one
    path target that was looked up directly."""
    if claim is not None:
        return [Path(os.path.normpath(p)).as_posix() for p in claim_paths(claim)]
    if target_path is not None:
        try:
            return [target_path.relative_to(root).as_posix()]
        except ValueError:
            return []
    return []


# ---------------------------------------------------------------------------
# Python: reverse import graph via ast
# ---------------------------------------------------------------------------

def _module_name(f: Path, root: Path) -> str:
    rel = f.relative_to(root).with_suffix("")
    parts = list(rel.parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _package_of(f: Path, root: Path) -> list[str]:
    """Dotted package parts the file itself lives in (for relative imports)."""
    rel = f.relative_to(root).with_suffix("")
    parts = list(rel.parts)
    if parts and parts[-1] == "__init__":
        return parts[:-1]
    return parts[:-1]


def python_reverse_imports(root: Path) -> dict[str, set[str]]:
    graph: dict[str, set[str]] = defaultdict(set)
    for f in root.rglob("*" + PY_SUFFIX):
        if "__pycache__" in f.parts:
            continue
        mod = _module_name(f, root)
        try:
            tree = ast.parse(f.read_text(errors="ignore"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    graph[alias.name].add(mod)
            elif isinstance(node, ast.ImportFrom):
                if node.level and node.level > 0:
                    pkg = _package_of(f, root)
                    up = node.level - 1
                    base_parts = pkg[: len(pkg) - up] if up <= len(pkg) else []
                    base = ".".join(base_parts)
                    resolved = f"{base}.{node.module}" if (base and node.module) else (node.module or base)
                else:
                    resolved = node.module
                if not resolved:
                    continue
                graph[resolved].add(mod)
                for alias in node.names:
                    graph[f"{resolved}.{alias.name}"].add(mod)
    return graph


def python_target_module(target: Path, root: Path) -> str:
    if target.is_dir():
        return ".".join(target.relative_to(root).parts)
    return _module_name(target, root)


def python_importers(target: Path, root: Path) -> set[str]:
    graph = python_reverse_imports(root)
    mod = python_target_module(target, root)
    result: set[str] = set()
    for key, files in graph.items():
        if key == mod or key.startswith(mod + "."):
            result |= files
    return {f.replace(".", "/") + PY_SUFFIX for f in result}


# ---------------------------------------------------------------------------
# Go: regex over import blocks (no `go list` dependency)
# ---------------------------------------------------------------------------

IMPORT_BLOCK_RE = re.compile(r"import\s*\(([^)]*)\)", re.DOTALL)
IMPORT_LINE_RE = re.compile(r"import\s+\"([^\"]+)\"")
QUOTED_RE = re.compile(r"\"([^\"]+)\"")


def _go_module_path(root: Path) -> str | None:
    go_mod = root / "go.mod"
    if not go_mod.is_file():
        return None
    m = re.search(r"^module\s+(\S+)", go_mod.read_text(errors="ignore"), re.MULTILINE)
    return m.group(1) if m else None


def go_import_path_for(target_dir: Path, root: Path) -> str | None:
    modpath = _go_module_path(root)
    if not modpath:
        return None
    rel = target_dir.relative_to(root).as_posix()
    return modpath if rel == "." else f"{modpath}/{rel}"


def go_importers(target: Path, root: Path) -> set[str]:
    target_dir = target if target.is_dir() else target.parent
    import_path = go_import_path_for(target_dir, root)
    fallback = target_dir.name  # used when go.mod is absent
    importers: set[str] = set()
    for f in root.rglob("*.go"):
        if f.parent == target_dir or (target.is_file() and f == target):
            continue
        text = f.read_text(errors="ignore")
        quoted: list[str] = []
        for block in IMPORT_BLOCK_RE.findall(text):
            quoted += QUOTED_RE.findall(block)
        quoted += IMPORT_LINE_RE.findall(text)
        for q in quoted:
            if import_path and q == import_path:
                importers.add(str(f.relative_to(root)))
                break
            if not import_path and (q == fallback or q.endswith("/" + fallback)):
                importers.add(str(f.relative_to(root)))
                break
    return importers


# ---------------------------------------------------------------------------
# TS/JS: regex over import/require specifiers, resolved relative to the file
# ---------------------------------------------------------------------------

JS_FROM_RE = re.compile(r"""(?:import|export)\s+(?:[^'";]*?from\s+)?['"]([^'"]+)['"]""")
JS_REQUIRE_RE = re.compile(r"""require\(\s*['"]([^'"]+)['"]\s*\)""")


def _js_specifiers(text: str) -> list[str]:
    return JS_FROM_RE.findall(text) + JS_REQUIRE_RE.findall(text)


def _js_normalize(p: Path) -> str:
    """Posix relative path, extension stripped, index-file collapsed to its dir."""
    s = p.as_posix()
    for suf in JS_SUFFIXES:
        if s.endswith(suf):
            s = s[: -len(suf)]
            break
    if s.endswith("/index") or s == "index":
        s = s[: -len("/index")] if s != "index" else "."
    return s


def js_importers(target: Path, root: Path) -> set[str]:
    target_id = _js_normalize(target.relative_to(root))
    importers: set[str] = set()
    for suf in JS_SUFFIXES:
        for f in root.rglob("*" + suf):
            if f == target:
                continue
            text = f.read_text(errors="ignore")
            for spec in _js_specifiers(text):
                if not spec.startswith("."):
                    continue
                resolved = (f.parent / spec).resolve()
                try:
                    rel = resolved.relative_to(root.resolve())
                except ValueError:
                    continue
                if _js_normalize(rel) == target_id:
                    importers.add(str(f.relative_to(root)))
                    break
    return importers


# ---------------------------------------------------------------------------
# Direct importers dispatch
# ---------------------------------------------------------------------------

def direct_importers(target: Path, root: Path) -> list[str]:
    found: set[str] = set()
    if target.is_dir():
        if any(target.rglob("*" + PY_SUFFIX)):
            found |= python_importers(target, root)
        if any(target.rglob("*" + GO_SUFFIX)):
            found |= go_importers(target, root)
        for idx in ("index.ts", "index.tsx", "index.js", "index.jsx"):
            if (target / idx).is_file():
                found |= js_importers(target / idx, root)
                break
    else:
        suffix = target.suffix
        if suffix == PY_SUFFIX:
            found |= python_importers(target, root)
        elif suffix == GO_SUFFIX:
            found |= go_importers(target, root)
        elif suffix in JS_SUFFIXES:
            found |= js_importers(target, root)
    return sorted(found)


# ---------------------------------------------------------------------------
# Text references: plain-substring grep for the target's name/path/module
# across config and docs that the import graph never sees (.env files,
# compose, docs, CI config -- nothing here is source code an ast/regex
# import scan would parse).
# ---------------------------------------------------------------------------

_TEXT_REF_GLOBS = (
    ".env*",
    "docs/**/*.md",
    "*.md",
    "compose*.y*ml",
    "docker-compose*.y*ml",
    "*.toml",
    "*.ini",
    "*.cfg",
    ".gitlab-ci.yml",
    ".github/workflows/*.yml",
)
_TEXT_REF_EXCLUDE_DIRS = {".git", ".worktrees", ".venv", "node_modules"}
_TEXT_REF_MAX_LINES = 30


def _text_ref_excluded(rel: Path) -> bool:
    parts = rel.parts
    if any(p in _TEXT_REF_EXCLUDE_DIRS for p in parts[:-1]):
        return True
    return parts[:2] == ("docs", "_human")


def _iter_text_ref_files(root: Path) -> list[Path]:
    found: set[Path] = set()
    for pattern in _TEXT_REF_GLOBS:
        for f in root.glob(pattern):
            if f.is_file() and not _text_ref_excluded(f.relative_to(root)):
                found.add(f)
    return sorted(found)


def _text_ref_candidates(paths: list[Path], root: Path) -> list[str]:
    candidates: set[str] = set()
    for p in paths:
        candidates.add(p.name)
        try:
            candidates.add(p.relative_to(root).as_posix())
        except ValueError:
            pass
        if p.suffix == PY_SUFFIX:
            candidates.add(_module_name(p, root))
    return sorted(c for c in candidates if c)


def text_references(paths: list[Path], root: Path) -> list[tuple[str, int, str]]:
    candidates = _text_ref_candidates(paths, root)
    if not candidates:
        return []
    hits: list[tuple[str, int, str]] = []
    for f in _iter_text_ref_files(root):
        rel = f.relative_to(root).as_posix()
        for lineno, line in enumerate(f.read_text(errors="ignore").splitlines(), 1):
            if any(c in line for c in candidates):
                snippet = line.strip()
                if len(snippet) > 120:
                    snippet = snippet[:120]
                hits.append((rel, lineno, snippet))
    return hits


def print_text_references(paths: list[Path], root: Path) -> None:
    hits = text_references(paths, root)
    print("text references:")
    if not hits:
        print("  (none)")
        return
    for rel, lineno, snippet in hits[:_TEXT_REF_MAX_LINES]:
        print(f"  {rel}:{lineno}: {snippet}")
    remaining = len(hits) - _TEXT_REF_MAX_LINES
    if remaining > 0:
        print(f"  ... {remaining} more")


# ---------------------------------------------------------------------------
# STATUS.yaml relationship lookup
# ---------------------------------------------------------------------------

def find_claim_by_id(data: dict, claim_id: str) -> dict | None:
    for c in data["claims"]:
        if c.get("id") == claim_id:
            return c
    return None


def find_claims_covering_path(data: dict, path: Path, root: Path) -> list[dict]:
    rel = path
    if rel.is_absolute():
        rel = rel.relative_to(root)
    matches = []
    for c in data["claims"]:
        for p in claim_paths(c):
            cp = Path(p)
            if cp == rel or cp in rel.parents or rel in cp.parents:
                matches.append(c)
                break
    return matches


def print_claim_links(data: dict, claim: dict) -> None:
    cid = claim["id"]
    status = claim.get("status") or claim.get("kind") or "?"
    print(f"declared claim relationships (STATUS.yaml claim: {cid}, status: {status}):")
    depends_on = claim.get("depends_on") or []
    if depends_on:
        print("  depends_on:")
        for d in depends_on:
            print(f"    - {d}")
    depended_by = [c["id"] for c in data["claims"] if cid in (c.get("depends_on") or [])]
    if depended_by:
        print("  depended on by:")
        for d in depended_by:
            print(f"    - {d}")
    edges = data.get("edges") or []
    out_edges = [e for e in edges if e.get("from") == cid]
    in_edges = [e for e in edges if e.get("to") == cid]
    if out_edges or in_edges:
        print("  edges:")
        for e in out_edges:
            print(f"    - -> {e.get('to')} (probe: {e.get('probe')}) -- run the probe to confirm this edge is live right now")
        for e in in_edges:
            print(f"    - <- {e.get('from')} (probe: {e.get('probe')}) -- run the probe to confirm this edge is live right now")
    if not depends_on and not depended_by and not out_edges and not in_edges:
        print("  (none declared)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("target", help="a path (relative to --root) or a STATUS.yaml claim id")
    ap.add_argument("--root", default=None)
    args = ap.parse_args()

    root = Path(args.root).resolve() if args.root else find_root(Path(__file__).parent)
    data = load_status(root)

    claim = find_claim_by_id(data, args.target)
    target_path: Path | None = None
    claim_targets: list[Path] = []

    if claim is not None:
        # a multi-path claim is one unit: union the importers of every path it names
        claim_targets = [root / p for p in claim_paths(claim) if (root / p).exists()]
        if claim_targets:
            target_path = claim_targets[0]
    else:
        candidate = Path(args.target)
        candidate = candidate if candidate.is_absolute() else root / candidate
        if candidate.exists():
            target_path = candidate
        else:
            print(f"error: {args.target!r} is neither an existing path under {root} "
                  f"nor a known STATUS.yaml claim id", file=sys.stderr)
            return 1

    if target_path is not None:
        if len(claim_targets) > 1:
            own = {str(t.relative_to(root)) for t in claim_targets}
            merged: set[str] = set()
            for t in claim_targets:
                merged.update(direct_importers(t, root))
            importers = sorted(merged - own)
            print(f"direct importers ({len(importers)}, union over {len(claim_targets)} claim paths, "
                  f"the claim's own files excluded):")
        else:
            importers = direct_importers(target_path, root)
            print(f"direct importers ({len(importers)}):")
        for imp in importers:
            print(f"  - {imp}")
    else:
        kind = claim.get("kind") if claim else None
        print(f"direct importers: n/a (claim has no path -- kind={kind})")

    if claim is None:
        rel = target_path.relative_to(root) if target_path else Path(args.target)
        covering = find_claims_covering_path(data, rel, root)
        if not covering:
            print("declared claim relationships (STATUS.yaml): no claim covers this path")
        else:
            for c in covering:
                print_claim_links(data, c)
    else:
        print_claim_links(data, claim)

    text_ref_paths = claim_targets if claim_targets else ([target_path] if target_path else [])
    print_text_references(text_ref_paths, root)

    if gt_hook_state is not None:
        try:
            rels = _ack_rels(root, claim, target_path)
            if rels:
                gt_hook_state.blast_ack(gt_hook_state.locate_git_dir(root), rels)
        except Exception:
            pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
