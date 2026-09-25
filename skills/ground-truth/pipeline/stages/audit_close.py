#!/usr/bin/env python3
"""Close an audit: set meta.last_audited and meta.last_audited_commit.

last_audited becomes run.date; last_audited_commit becomes the full HEAD
sha of run.repo -- the commit the audit actually looked at, so a later
audit_scope can compare against it. Requires explicit --reviewed and a
fresh full verification. --force allows initial review without a scope
file; it never bypasses verification. An existing scope must match HEAD.
Text substitution preserves unrelated lines and comments. Writes only
with --write; the caller attests semantic review.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from gt_lib.git import _run  # noqa: E402
from gt_lib.paths import load_run, stage_dir  # noqa: E402
from stages.audit_scope import verify  # noqa: E402

_LINE_RE = re.compile(r"^(\s*last_audited:\s*)(['\"]?)\d{4}-\d{2}-\d{2}\2(.*)$", re.M)
_COMMIT_LINE_RE = re.compile(
    r"^(\s*)last_audited_commit:\s*(['\"]?)[0-9a-fA-F]{7,40}\2[ \t]*(#.*)?$", re.M
)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run", required=True)
    parser.add_argument("--status", help="path to STATUS.yaml (default: run.repo/STATUS.yaml)")
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--reviewed", action="store_true", help="confirm substantive review of the current scope")
    parser.add_argument("--force", action="store_true", help="allow initial audit with no scope report; does not bypass verification")
    args = parser.parse_args(argv)
    if not args.reviewed:
        print("refusing: a quick check is not an audit; --reviewed requires substantive review of the current scope")
        return 1
    run = load_run(args.run)
    status_path = Path(args.status) if args.status else run.repo / "STATUS.yaml"

    scope_path = stage_dir(run, "audit") / "audit-scope.json"
    if not args.force:
        if not scope_path.exists():
            print(f"refusing: {scope_path} missing (run audit_scope first, or --force)"); return 1
        scope = json.loads(scope_path.read_text(encoding="utf-8"))
        if scope.get("head") != _run(run.repo, ["rev-parse", "HEAD"]).strip():
            print("refusing: HEAD changed since audit_scope; refresh the scope and review the new changes"); return 1

    if status_path.resolve() != (run.repo / "STATUS.yaml").resolve():
        print("refusing: only the repository STATUS.yaml can receive the audit checkpoint"); return 1
    current = verify(run)
    if current["exit"] != 0:
        print(f"refusing: current verification failed (exit {current['exit']}, {current['fail']} failures)")
        return 1

    text = status_path.read_text(encoding="utf-8")
    m = _LINE_RE.search(text)
    if not m:
        print("no meta.last_audited line found"); return 1
    quote = m.group(2)
    indent = re.match(r"[ \t]*", m.group(0)).group()
    old_date_line = m.group(0)
    new_date_line = f"{m.group(1)}{quote}{run.date}{quote}{m.group(3)}"
    text2 = text[:m.start()] + new_date_line + text[m.end():]

    head = _run(run.repo, ["rev-parse", "HEAD"]).strip()
    new_commit_line = f"{indent}last_audited_commit: {quote}{head}{quote}"
    commit_m = _COMMIT_LINE_RE.search(text2)
    if commit_m:
        old_commit_line = commit_m.group(0)
        if commit_m.group(3):
            new_commit_line += "  " + commit_m.group(3)
        text3 = text2[:commit_m.start()] + new_commit_line + text2[commit_m.end():]
    else:
        old_commit_line = None
        insert_at = text2.index("\n", m.start() + len(new_date_line)) + 1
        text3 = text2[:insert_at] + new_commit_line + "\n" + text2[insert_at:]

    print(f"{old_date_line.strip()}  ->  {new_date_line.strip()}")
    print(f"{old_commit_line.strip() if old_commit_line else '(none)'}  ->  {new_commit_line.strip()}")

    changed = old_date_line != new_date_line or old_commit_line != new_commit_line
    if args.write and changed:
        status_path.write_text(text3, encoding="utf-8")
        print("written")
    elif not args.write:
        print("dry-run, nothing written")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
