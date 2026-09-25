#!/usr/bin/env python3
"""Probe: a Python module can be imported and exposes a given attribute.

Usage: example_probe.py <module.dotted.path> <attribute_name>
Exit 0: attribute exists. Exit 1: module imports but attribute is missing.
Exit 77: module cannot be imported (prerequisite missing) -- treated as skip.

A different shape of probe re-runs the whole unit suite as the check
itself, rather than testing one attribute. Run it through the current
interpreter, not a bare `pytest` off PATH, and disable the cache plugin so
a stale rewrite of a .pyc under __pycache__ cannot stand in for a real
result:

    subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", ...],
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )

A probe that checks a pinned list of pipeline job names is edited in the
same change that adds or renames a job, not as a follow-up -- otherwise it
either misses the new job or fails on a rename that changed nothing real.

A probe that shells out to a binary the repository does not vendor (curl,
sha256sum, ansible, ssh) checks for it first and exits 77 when it is
missing -- `shutil.which("curl") or sys.exit(77)` -- or, when the script
under test only tests for the binary's presence before the branch being
probed, puts a stub on PATH. Slim and alpine python images ship neither
curl nor git: the developer's machine is not the CI image. A probe that
reads where a setting comes from (ansible-config dump and the like) strips
the matching ambient variables first: CI files export them globally, and
an env origin is not the file origin the claim names.

A probe that walks the repo tree (counting files, grepping for a pattern,
checking every module under a package) uses iter_repo_files below instead
of os.walk/rglob directly -- a stray checkout under .worktrees/ or a venv
is not part of the repo the claim is about, and reading it produces a
false fail.
"""
import importlib
import sys
from pathlib import Path

_SKIP_DIRS = {".git", ".worktrees", ".venv", "node_modules", "__pycache__", ".mypy_cache", ".pytest_cache"}


def iter_repo_files(root, suffixes=None):
    """Yield files under root, skipping vcs/worktree/venv/cache dirs at any
    depth. Pass suffixes (e.g. (".py",)) to filter by extension."""
    root = Path(root)
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if _SKIP_DIRS & set(path.relative_to(root).parts):
            continue
        if suffixes and path.suffix not in suffixes:
            continue
        yield path


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: example_probe.py <module> <attribute>", file=sys.stderr)
        return 77
    module_name, attr = argv
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        print(f"skip: cannot import {module_name}: {exc}", file=sys.stderr)
        return 77
    if not hasattr(module, attr):
        print(f"fail: {module_name} has no attribute {attr!r}", file=sys.stderr)
        return 1
    print(f"ok: {module_name}.{attr} exists")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
