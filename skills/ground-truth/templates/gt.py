#!/usr/bin/env python3
"""ground-truth commands for this repository.

Usage: python3 tools/ground_truth/gt.py <command> [args]

  migrate   move STATUS.yaml into the sharded .ground-truth/ layout (gt_migrate.py)
  doctor    check that the ground-truth hooks can run here (gt_doctor.py)

A command imports its module only when it runs, so doctor works without PyYAML.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

COMMANDS = {"migrate": "gt_migrate", "doctor": "gt_doctor"}


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in COMMANDS:
        print(__doc__, file=sys.stderr)
        return 2
    return importlib.import_module(COMMANDS[argv[0]]).main(argv[1:])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
