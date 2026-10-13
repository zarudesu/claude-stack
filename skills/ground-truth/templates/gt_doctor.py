#!/usr/bin/env python3
"""Check that the ground-truth hooks can run in this checkout.

Usage: python3 tools/ground_truth/gt_doctor.py [--skill DIR]

Stdlib only, so it runs under any python3 even when the hook interpreter is
the broken part. Checks the interpreter the hooks resolve (GT_PYTHON, the
absolute path the installer pinned in the git dir, .venv/bin/python, python3
from PATH -- the last one is reported, the hooks must not depend on PATH
silently) and that it imports PyYAML; that every hook is registered in the
project settings and its script exists; that a CI job runs the verifier.
--skill DIR also compares installed hook and tool files with DIR/templates.
Prints one summary line, then one line per problem; exit 1 on any problem.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PIN = "ground-truth-python"
HOOKS = ("ground-truth-sync-check.sh", "ground-truth-session-start.sh", "ground-truth-edit-guard.sh")
SETTINGS = (".claude/settings.json", ".claude/settings.local.json")  # gt-allow: real settings path, not prose
CI_FILES = (".gitlab-ci.yml", ".github/workflows/*.yml", ".github/workflows/*.yaml")
CI_VERIFY = "tools/ground_truth/verify.py --mode=full"
FIX = "re-run the ground-truth install (i9_install --write)"


def interpreter() -> tuple[str, str]:
    """(python, where it came from), in the order the hook scripts use."""
    if os.environ.get("GT_PYTHON"):
        return os.environ["GT_PYTHON"], "GT_PYTHON"
    git_dir = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
                             cwd=ROOT, capture_output=True, text=True).stdout.strip()
    pin = Path(git_dir) / PIN
    if git_dir and pin.is_file() and pin.read_text().strip():
        return pin.read_text().strip(), str(pin)
    if os.access(ROOT / ".venv/bin/python", os.X_OK):
        return str(ROOT / ".venv/bin/python"), ".venv"
    return "python3", "PATH"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--skill", type=Path, help="skill directory to compare installed files with")
    args = parser.parse_args(argv)
    problems = []

    py, source = interpreter()
    if source == "PATH":
        problems.append(f"hook interpreter is python3 from PATH, not pinned; fix: {FIX}")
    try:
        cp = subprocess.run([py, "-c", "import yaml"], capture_output=True, text=True)
        err = (cp.stderr.strip().splitlines() or [f"exit {cp.returncode}"])[-1] if cp.returncode else ""
    except OSError as exc:
        err = str(exc)
    if err:
        problems.append(f"hook interpreter {py} cannot import yaml: {err}; fix: {py} -m pip install pyyaml")

    settings = "".join((ROOT / s).read_text() for s in SETTINGS if (ROOT / s).is_file())
    for name in HOOKS:
        m = re.search(r"bash (\S*" + re.escape(name) + ")", settings)
        if not m:
            problems.append(f"{name} not registered in {' or '.join(SETTINGS)}; fix: {FIX}")
        elif not (ROOT / m.group(1)).is_file():
            problems.append(f"{m.group(1)} is registered but missing; fix: {FIX}")
        elif args.skill and (ROOT / m.group(1)).read_bytes() != (args.skill / "templates" / name).read_bytes():
            problems.append(f"{m.group(1)} differs from the skill template; fix: {FIX}")
    if args.skill:
        for path in sorted((ROOT / "tools/ground_truth").glob("*.py")):
            template = args.skill / "templates" / path.name
            if template.is_file() and template.read_bytes() != path.read_bytes():
                problems.append(f"tools/ground_truth/{path.name} differs from the skill template; fix: {FIX}")

    ci = [p for pattern in CI_FILES for p in ROOT.glob(pattern)]
    if not any(CI_VERIFY in p.read_text() for p in ci):
        problems.append(f"no CI job runs {CI_VERIFY}; fix: {FIX}")

    verdict = "ok" if not problems else f"{len(problems)} problem(s)"
    print(f"ground-truth doctor: {verdict} (hook python: {py}, from {source})")
    for problem in problems:
        print(f"- {problem}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
