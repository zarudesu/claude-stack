#!/usr/bin/env python3
"""ground-truth commands for this repository.

Usage: python3 tools/ground_truth/gt.py <command> [args]

  context   pointers to the claims behind --path P... or --area A (gt_packet.py)
  show      one claim, area:<id>, or the claims of a path, in full (gt_packet.py)
  sync      verify.py --mode=sync, then queue its deferred warnings as the Stop hook does
  audit     verify.py --mode=full, plus gt_context.py check when model.yaml exists
  doctor    check that the ground-truth hooks can run here (gt_doctor.py)
  batch     apply a batch review: --verdicts FILE (gt_pending.py batch)
  pending   record|gate|summary|report (gt_pending.py)
  remap     shift file:line references in the contract (remap_line_refs.py)
  refresh   record a review receipt for model.yaml areas (gt_context.py refresh)
  blast     what a path or claim id touches (blast_radius.py)
  mutate    mutation self-check of the canary claims (gt_mutation_selfcheck.py)
  migrate   move STATUS.yaml into the sharded .ground-truth/ layout (gt_migrate.py)

A command imports its module only when it runs, so doctor works without PyYAML.
"""
from __future__ import annotations

import contextlib
import importlib
import io
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))


def _root() -> Path:
    return importlib.import_module("gt_store").find_root(HERE) or Path.cwd()


def _sync(argv: list[str]) -> int:
    if argv:
        print("usage: gt sync", file=sys.stderr)
        return 2
    verify, gt_pending = importlib.import_module("verify"), importlib.import_module("gt_pending")
    with contextlib.redirect_stdout(io.StringIO()) as out:
        rc = verify.main(["--mode=sync", "--show-warn"])
    print(out.getvalue(), end="")
    if rc <= 1:
        root = _root()
        try:
            gt_pending.record(root, gt_pending.status_findings(root, out.getvalue().splitlines()),
                              set(gt_pending.STATUS_CLASSES), "gt-sync")
        except gt_pending.Unreadable as exc:
            print(f"ground-truth pending: {exc}", file=sys.stderr)
    return rc


def _audit(argv: list[str]) -> int:
    rc = importlib.import_module("verify").main(["--mode=full", *argv])
    if (HERE / "gt_context.py").is_file():
        gt_context = importlib.import_module("gt_context")
        model_root = gt_context.discover(_root())
        if model_root is not None:
            rc = max(rc, gt_context.main(["--root", str(model_root), "check"]))
    return rc


def _blast(argv: list[str]) -> int:
    sys.argv = ["gt blast", *argv]  # blast_radius.main reads sys.argv
    return importlib.import_module("blast_radius").main()


def _pending(*head: str):
    return lambda argv: importlib.import_module("gt_pending").main(["--root", str(_root()), *head, *argv])


def _call(module: str, function: str = "main", *head: str):
    return lambda argv: getattr(importlib.import_module(module), function)([*head, *argv])


COMMANDS = {
    "context": _call("gt_packet"),
    "show": _call("gt_packet", "show_main"),
    "sync": _sync,
    "audit": _audit,
    "doctor": _call("gt_doctor"),
    "batch": _pending("batch"),
    "pending": _pending(),
    "remap": _call("remap_line_refs"),
    "refresh": _call("gt_context", "main", "refresh"),
    "blast": _blast,
    "mutate": _call("gt_mutation_selfcheck"),
    "migrate": _call("gt_migrate"),
}


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in COMMANDS:
        print(__doc__, file=sys.stderr)
        return 2
    return COMMANDS[argv[0]](argv[1:])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
