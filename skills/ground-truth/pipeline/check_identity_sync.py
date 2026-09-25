#!/usr/bin/env python3
"""Check that every pipeline/workflows/*.js embeds the shared identity text unchanged.

Compares each file's `const IDENTITY = \\`...\\`` template literal against
gt_lib.banned_words.IDENTITY_TEXT (whitespace-normalised) and flags any use
of Date.now(, Math.random( or new Date( -- all three break workflow resume.
Skill-internal: no --run, no other arguments.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from gt_lib.banned_words import IDENTITY_TEXT  # noqa: E402

_IDENTITY_LITERAL_RE = re.compile(r"const IDENTITY\s*=\s*`(.*?)`", re.S)
_BANNED_CALLS = ("Date.now(", "Math.random(", "new Date(")


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def check_file(path: Path) -> list[str]:
    """Return a list of problem descriptions for one workflow file (empty if clean)."""
    text = path.read_text(encoding="utf-8")
    problems = []

    m = _IDENTITY_LITERAL_RE.search(text)
    if not m:
        problems.append("no `const IDENTITY = ...` template literal found")
    elif _normalize(m.group(1)) != _normalize(IDENTITY_TEXT):
        problems.append("IDENTITY literal does not match gt_lib.banned_words.IDENTITY_TEXT")

    for call in _BANNED_CALLS:
        if call in text:
            problems.append(f"contains {call} (breaks workflow resume)")

    return problems


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Check every pipeline/workflows/*.js embeds the shared identity text unchanged.",
    )
    parser.parse_args(argv)

    root = Path(__file__).resolve().parent / "workflows"
    files = sorted(root.glob("*.js")) if root.is_dir() else []

    failed = False
    for path in files:
        problems = check_file(path)
        if problems:
            failed = True
            print(f"FAIL {path.name}: {'; '.join(problems)}")
        else:
            print(f"OK {path.name}")

    if not files:
        print("no pipeline/workflows/*.js files found")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
