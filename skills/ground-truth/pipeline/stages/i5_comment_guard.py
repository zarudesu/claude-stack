#!/usr/bin/env python3
"""Verify that a set of files changed only in comments and docstrings.

For each file, compares its HEAD content to its working-tree content
with comments and docstrings stripped: .py via the AST (leading
docstrings removed, then ast.dump of the rest), .yml/.yaml via a YAML
loader that tolerates unknown tags, .j2 by stripping "{# ... #}" blocks
and comment-only lines, everything else recognized in COMMENT_PREFIXES
by a generic fallback that drops lines which are pure comments (an
inline trailing comment on a code line is not stripped, so editing one
is reported as CHANGED-STRUCTURE rather than silently passed -- the
guard is meant to be conservative, not exhaustive). An extension with no
comment convention registered is UNSUPPORTED. Read-only: never writes
anything, never runs a state-changing git command.
"""
from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from gt_lib.git import diff_names, show  # noqa: E402
from gt_lib.paths import load_run  # noqa: E402

import yaml

COMMENT_PREFIXES = {
    ".sh": ("#",),
    ".bash": ("#",),
    ".cfg": ("#", ";"),
    ".ini": ("#", ";"),
    ".toml": ("#",),
    ".service": ("#", ";"),
    ".timer": ("#", ";"),
    ".socket": ("#", ";"),
    ".target": ("#", ";"),
    ".yml": ("#",),
    ".yaml": ("#",),
    ".go": ("//",),
    ".js": ("//",),
    ".ts": ("//",),
    ".rs": ("//",),
}

_JINJA_COMMENT_RE = re.compile(r"\{#.*?#\}", re.S)


def py_skeleton(src: str) -> str | None:
    """Return src's AST with leading docstrings removed, or None on a syntax error."""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return None
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                node.body = body[1:]
    return ast.dump(tree, include_attributes=False)


class _AnyTagLoader(yaml.SafeLoader):
    """A SafeLoader that accepts any tag as an opaque scalar/sequence/mapping."""


def _any_ctor(loader, tag_suffix, node):
    if isinstance(node, yaml.ScalarNode):
        value = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node)
    else:
        value = loader.construct_mapping(node)
    # keep the tag in the skeleton -- two values that differ only by tag
    # (e.g. !vault vs !othertag) must not produce the same repr()
    return ("!" + tag_suffix, value)


_AnyTagLoader.add_multi_constructor("", _any_ctor)


def yaml_skeleton(src: str) -> str | None:
    """Return a repr of every YAML document in src (comments carry no node), or None if it fails to parse."""
    try:
        docs = list(yaml.load_all(src, Loader=_AnyTagLoader))
    except yaml.YAMLError:
        return None
    return repr(docs)


def j2_skeleton(src: str) -> str:
    """Strip Jinja {# ... #} comment blocks and comment-only lines."""
    src = _JINJA_COMMENT_RE.sub("", src)
    lines = [ln for ln in src.split("\n") if not ln.strip().startswith("#")]
    return "\n".join(lines)


def generic_skeleton(src: str, prefixes: tuple[str, ...]) -> str:
    """Drop lines that are pure comments (any other change is reported as CHANGED-STRUCTURE)."""
    lines = [ln for ln in src.split("\n") if not any(ln.strip().startswith(p) for p in prefixes)]
    return "\n".join(lines)


def skeleton_of(path: str, src: str):
    """Return (skeleton, verb_if_it_cannot_be_computed_or_None)."""
    ext = Path(path).suffix
    if ext == ".py":
        sk = py_skeleton(src)
        return (sk, None) if sk is not None else (None, "CHANGED-STRUCTURE")
    if ext in (".yml", ".yaml"):
        sk = yaml_skeleton(src)
        return (sk, None) if sk is not None else (None, "CHANGED-STRUCTURE")
    if ext == ".j2":
        return j2_skeleton(src), None
    if ext in COMMENT_PREFIXES:
        return generic_skeleton(src, COMMENT_PREFIXES[ext]), None
    return None, "UNSUPPORTED"


def check_file(repo: Path, rel: str) -> tuple[str, str]:
    """Return (verb, detail) for one file: OK, CHANGED-STRUCTURE, or UNSUPPORTED."""
    head_text = show(repo, "HEAD", rel)
    if head_text is None:
        return "UNSUPPORTED", "no HEAD version (new file)"
    wt_path = repo / rel
    if not wt_path.is_file():
        return "UNSUPPORTED", "deleted in the working tree"
    wt_text = wt_path.read_text(encoding="utf-8", errors="surrogateescape")

    head_sk, head_bad = skeleton_of(rel, head_text)
    if head_bad:
        if head_bad == "UNSUPPORTED":
            return "UNSUPPORTED", "no comment convention registered for this extension"
        return "CHANGED-STRUCTURE", "HEAD version does not parse"
    wt_sk, wt_bad = skeleton_of(rel, wt_text)
    if wt_bad:
        if wt_bad == "UNSUPPORTED":
            return "UNSUPPORTED", "no comment convention registered for this extension"
        return "CHANGED-STRUCTURE", "working-tree version does not parse"

    if head_sk == wt_sk:
        return "OK", ""
    return "CHANGED-STRUCTURE", "non-comment content differs"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run", required=True, help="path to run.json")
    parser.add_argument("--file", action="append", dest="files", metavar="PATH",
                         help="repo-relative path to check (repeatable); default: every modified tracked file")
    args = parser.parse_args(argv)

    run = load_run(args.run)
    files = args.files if args.files else diff_names(run.repo)

    bad = 0
    for rel in files:
        verb, detail = check_file(run.repo, rel)
        if verb != "OK":
            bad += 1
        line = f"{verb:16s} {rel}"
        if detail:
            line += f"  ({detail})"
        print(line)

    if not files:
        print("no files to check")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
