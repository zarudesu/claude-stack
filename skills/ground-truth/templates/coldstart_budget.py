#!/usr/bin/env python3
"""Sum bytes of a declared file list -- used to measure a cold-start bundle
"before" (a repo's own current onboarding instruction) and "after"
(CLAUDE.md + .claude/rules/ground-truth.md -- gt-allow: real file names, not identity prose). STATUS.yaml is never part of
the bundle: it is grepped, not read whole -- do not pass it as an argument.
Both lists are literal file paths, never guessed or discovered by walking
the tree.

Usage:
    python3 tools/ground_truth/coldstart_budget.py FILE...

Prints one line per file (bytes, path) and a total line. Missing files are
skipped with a note on stderr, not counted, so the total never silently
under- or over-states the bundle. The total/4 annotation is an approximate
token count for a human reader -- never the acceptance criterion itself
(bytes are, because they need no assumption about tokenizer).
"""
from __future__ import annotations

import sys
from pathlib import Path


def budget(files: list[Path]) -> tuple[int, list[tuple[Path, int]]]:
    sizes: list[tuple[Path, int]] = []
    total = 0
    for f in files:
        if not f.exists():
            print(f"skip (not found): {f}", file=sys.stderr)
            continue
        size = f.stat().st_size
        sizes.append((f, size))
        total += size
    return total, sizes


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: coldstart_budget.py FILE...", file=sys.stderr)
        return 1
    files = [Path(a) for a in sys.argv[1:]]
    total, sizes = budget(files)
    for f, size in sizes:
        print(f"{size:>10}  {f}")
    print(f"{total:>10}  TOTAL")
    print(f"{total // 4:>10}  TOTAL/4 (approximate token annotation, not a criterion)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
