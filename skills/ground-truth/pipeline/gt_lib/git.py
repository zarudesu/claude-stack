"""Read-only git helpers shared by pipeline stages.

Every function takes repo (a Path to a git worktree) as its first
argument and never runs a state-changing git command. hunks() reuses the
exact hunk-header parsing of templates/remap_line_refs.py's parse_diff --
several later stages (line-drift reconciliation) depend on its precise
off-by-one handling of pure insertions and deletions (an old_len or
new_len of 0 in the @@ header), so that parsing is not rewritten here.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))?\s\+(\d+)(?:,(\d+))?\s@@")
_PLUS_PATH_RE = re.compile(r"^\+\+\+ b/(.+)$")

_head_cache: dict = {}

# Force English output regardless of the caller's locale. show() tells a
# missing path apart from every other failure by matching substrings of
# git's fatal message; a translated locale would otherwise send those
# messages through gettext and break that classification.
_GIT_ENV = dict(os.environ, LC_ALL="C", LANGUAGE="C")


def _run(repo: Path, args: list[str]) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, env=_GIT_ENV,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def ls_files(repo: Path, *globs: str) -> list[str]:
    """List tracked files, optionally restricted to pathspec globs."""
    args = ["ls-files"]
    if globs:
        args += ["--", *globs]
    return [line for line in _run(repo, args).split("\n") if line]


_MISSING_PATH_MARKERS = ("does not exist in", "exists on disk, but not in")


def show(repo: Path, ref: str, path: str) -> str | None:
    """Return path's content at ref, or None if the path does not exist there.

    Any other failure -- a bad ref, a non-repo directory, a detached or
    unborn HEAD -- raises RuntimeError like every other function here.
    """
    result = subprocess.run(
        ["git", "show", f"{ref}:{path}"], cwd=repo, capture_output=True, text=True,
        env=_GIT_ENV,
    )
    if result.returncode != 0:
        stderr = result.stderr.strip()
        if any(marker in stderr for marker in _MISSING_PATH_MARKERS):
            return None
        raise RuntimeError(f"git show {ref}:{path} failed: {stderr}")
    return result.stdout


def diff_names(repo: Path, base: str = "HEAD") -> list[str]:
    """List paths with a working-tree difference from base."""
    return [line for line in _run(repo, ["diff", "--name-only", base]).split("\n") if line]


def hunks(repo: Path, base: str = "HEAD", path: str | None = None) -> dict:
    """Parse `git diff -U0 --diff-filter=M` into per-file hunk ranges.

    Returns {path: [(old_start, old_len, new_start, new_len), ...]} for
    files that still exist in the working tree. old_len/new_len is 1 when
    the @@ header omits a count (a single-line hunk); either can be 0 for
    a pure insertion or deletion.
    """
    out = _run(repo, ["diff", "-U0", "--diff-filter=M", base, "--", path or "."])
    files: dict = {}
    current = None
    for line in out.splitlines():
        if line.startswith("diff --git a/"):
            current = None
        elif (m := _PLUS_PATH_RE.match(line)):
            current = m.group(1) if m.group(1) != "/dev/null" else None
            if current is not None:
                files.setdefault(current, [])
        elif current is not None and (m := _HUNK_RE.match(line)):
            a, b, c, d = m.groups()
            files[current].append((int(a), int(b) if b else 1, int(c), int(d) if d else 1))
    return {p: h for p, h in files.items() if (Path(repo) / p).is_file()}


def removed_lines(repo: Path, base: str = "HEAD") -> dict:
    """Lines deleted from each modified file between base and the working tree.

    Returns {path: [text, ...]} from `git diff -U0 --diff-filter=M`, the
    leading "-" stripped, for files that still exist in the working tree.
    """
    out = _run(repo, ["diff", "-U0", "--diff-filter=M", base, "--", "."])
    files: dict = {}
    current = None
    for line in out.splitlines():
        if line.startswith("diff --git a/"):
            current = None
        elif (m := _PLUS_PATH_RE.match(line)):
            current = m.group(1) if m.group(1) != "/dev/null" else None
            if current is not None:
                files.setdefault(current, [])
        elif current is not None and line.startswith("-") and not line.startswith("---"):
            files[current].append(line[1:])
    return {p: ls for p, ls in files.items() if (Path(repo) / p).is_file()}


def head_lines(repo: Path, path: str) -> list[str]:
    """Return path's lines at HEAD, cached per (repo, path) for this process."""
    key = (str(repo), path)
    if key not in _head_cache:
        text = show(repo, "HEAD", path)
        _head_cache[key] = text.split("\n") if text is not None else []
    return _head_cache[key]
