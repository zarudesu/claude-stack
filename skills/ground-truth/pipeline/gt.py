#!/usr/bin/env python3
"""Dispatch to a pipeline stage by name.

Usage:
    gt.py <stage> [args...]   run pipeline/stages/<stage>.py's main(argv)
    gt.py --list              list every stage with its one-line summary
    gt.py --help              print this usage plus the stage list
"""
from __future__ import annotations

import importlib
import re
import sys
from pathlib import Path

PIPELINE_ROOT = Path(__file__).resolve().parent
STAGES_DIR = PIPELINE_ROOT / "stages"

# Run-phase order for --list sorting (dots dropped, so I.3.5 is "i35").
PHASE_ORDER = ["i1", "i2", "i35", "i4", "i5", "i6", "i9", "i10"]

_PHASE_PREFIX_RE = re.compile(r"^(i\d+)_")


def _phase_of(stage_name: str) -> str:
    m = _PHASE_PREFIX_RE.match(stage_name)
    return m.group(1) if m else stage_name


def _sort_key(stage_name: str):
    phase = _phase_of(stage_name)
    idx = PHASE_ORDER.index(phase) if phase in PHASE_ORDER else len(PHASE_ORDER)
    return (idx, stage_name)


def _stage_names() -> list[str]:
    if not STAGES_DIR.is_dir():
        return []
    names = [p.stem for p in STAGES_DIR.glob("*.py") if p.stem != "__init__"]
    return sorted(names, key=_sort_key)


def _first_docline(stage_name: str) -> str:
    text = (STAGES_DIR / f"{stage_name}.py").read_text(encoding="utf-8")
    m = re.search(r'"""(.*?)(?:\n|""")', text)
    return m.group(1).strip() if m else ""


def _print_list() -> None:
    for name in _stage_names():
        print(f"{name}: {_first_docline(name)}")


def main(argv: list[str]) -> int:
    if str(PIPELINE_ROOT) not in sys.path:
        sys.path.insert(0, str(PIPELINE_ROOT))

    if not argv or argv[0] == "--help":
        print(__doc__)
        _print_list()
        return 0

    if argv[0] == "--list":
        _print_list()
        return 0

    stage, rest = argv[0], argv[1:]
    if stage not in _stage_names():
        print(f"unknown stage: {stage}", file=sys.stderr)
        _print_list()
        return 2

    mod = importlib.import_module(f"stages.{stage}")
    return mod.main(rest)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
