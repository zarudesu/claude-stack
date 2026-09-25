#!/usr/bin/env python3
"""Revert a status raise that the raise-vote workflow's refuters rejected.

Reads a raise-vote.js result (--votes FILE: {votes: [{id, verdicts}], ...})
and, for every claim whose refuters were a strict majority "refuted",
restores that claim's block and every debt block that references it
(ref: <claim id>) verbatim from a pre-raise STATUS.yaml backup (--backup
FILE):

- a claim/debt block present in both the backup and the current file is
  replaced by the backup's version (this restores status/note/description/
  state to what they were before the raise);
- a debt block present in the backup but missing from the current file
  (removed while making the case for the raise) is reinserted;
- a debt block present in the current file but absent from the backup (a
  new debt added while making the case for the raise) is removed.

No document is edited -- only STATUS.yaml. This is a generic backup
restore, not a hand-written fix: it undoes the raise as a whole rather
than reasoning about which specific edits were disputed.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from gt_lib import paths  # noqa: E402
from gt_lib.paths import load_run  # noqa: E402
from gt_lib.yaml_edit import blocks  # noqa: E402


def majority_refuted(verdicts: list[dict]) -> bool:
    if not verdicts:
        return False
    n_refuted = sum(1 for v in verdicts if v.get("refuted"))
    return n_refuted * 2 > len(verdicts)


def _section_span(t: str, key: str) -> tuple[int, int] | None:
    """Return (start, end) offsets of top-level section `key`'s body in t, or None."""
    m = re.search(r"^" + re.escape(key) + r":[ \t]*(?:#.*)?$\n", t, re.M)
    if not m:
        return None
    start = m.end()
    nxt = re.search(r"^[A-Za-z_][A-Za-z0-9_]*:", t[start:], re.M)
    end = start + nxt.start() if nxt else len(t)
    return start, end


def _insert_into_section(t: str, key: str, block_text: str) -> str | None:
    """Insert block_text as the last entry of top-level section `key`.

    Anchors on the section's own header and its last existing entry (found
    fresh in t, not from a stale snapshot) so the block always lands inside
    the right section instead of wherever an unrelated block happens to sit.
    Returns None if the section header is not found.
    """
    span = _section_span(t, key)
    if span is None:
        return None
    start, end = span
    body = t[start:end]
    entries = list(blocks(body).values())  # document order within the section
    offset = body.rfind(entries[-1]) + len(entries[-1]) if entries else 0
    insert_at = start + offset
    return t[:insert_at] + block_text + t[insert_at:]


def revert_one(t: str, claim_id: str, cur: dict, backup: dict) -> tuple[str, list[str]]:
    """Restore claim_id and every debt referencing it from backup into t. Returns (t, notes)."""
    notes = []
    ids = {claim_id} | {i for i, b in cur.items() if b["ref"] == claim_id} | {i for i, b in backup.items() if b["ref"] == claim_id}
    for i in sorted(ids):
        in_cur = i in cur
        in_backup = i in backup
        if in_backup and in_cur:
            if cur[i]["text"] != backup[i]["text"]:
                t = t.replace(cur[i]["text"], backup[i]["text"], 1)
                notes.append(f"restored {i}")
        elif in_backup and not in_cur:
            # was removed while making the case for the raise -- put it back
            # as the last entry of its own section (debt if it carries a
            # ref field, claims otherwise), never spliced after an
            # unrelated block or appended past the last top-level key.
            key = "debt" if backup[i]["ref"] is not None else "claims"
            new_t = _insert_into_section(t, key, backup[i]["text"])
            if new_t is None:
                notes.append(f"FAILED to reinsert {i}: no {key}: section found")
            else:
                t = new_t
                notes.append(f"reinserted {i}")
        elif in_cur and not in_backup:
            t = t.replace(cur[i]["text"], "", 1)
            notes.append(f"removed {i}")
    return t, notes


def _ref_of(block_text: str) -> str | None:
    for line in block_text.splitlines():
        s = line.strip()
        if s.startswith("ref:"):
            return s[len("ref:"):].strip().strip("'\"")
    return None


def _index(text: str) -> dict:
    """{id: {text, ref}} for every block in a STATUS.yaml text."""
    return {k: {"text": v, "ref": _ref_of(v)} for k, v in blocks(text).items()}


def build(cur_text: str, backup_text: str, votes: list[dict]) -> dict:
    cur = _index(cur_text)
    backup = _index(backup_text)
    t = cur_text
    reverted = []
    skipped = []
    for entry in votes:
        i = entry["id"]
        if not majority_refuted(entry.get("verdicts") or []):
            skipped.append(i)
            continue
        t, notes = revert_one(t, i, cur, backup)
        reverted.append({"id": i, "notes": notes})
    return {"text": t, "reverted": reverted, "skipped": skipped}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Revert majority-refuted status raises from a STATUS.yaml backup.")
    parser.add_argument("--run", required=True, help="path to run.json")
    parser.add_argument("--votes", required=True, help="path to the raise-vote.js result JSON")
    parser.add_argument("--backup", required=True, help="path to the pre-raise STATUS.yaml backup")
    parser.add_argument("--status", help="path to STATUS.yaml (default: run.repo/STATUS.yaml)")
    parser.add_argument("--write", action="store_true", help="write STATUS.yaml")
    parser.add_argument("--dry-run", action="store_true", help="report only, write nothing (default)")
    args = parser.parse_args(argv)

    run = load_run(args.run)
    write = args.write and not args.dry_run

    status_path = Path(args.status) if args.status else run.repo / "STATUS.yaml"
    if not status_path.is_file():
        print(f"error: STATUS.yaml not found at {status_path}", file=sys.stderr)
        return 1
    backup_path = Path(args.backup)
    if not backup_path.is_file():
        print(f"error: backup not found at {backup_path}", file=sys.stderr)
        return 1

    raw = json.loads(Path(args.votes).read_text(encoding="utf-8"))
    votes = raw.get("votes", raw if isinstance(raw, list) else [])

    result = build(
        status_path.read_text(encoding="utf-8"),
        backup_path.read_text(encoding="utf-8"),
        votes,
    )

    for r in result["reverted"]:
        print(f"reverted {r['id']}: {', '.join(r['notes']) or 'no change (already matches backup)'}")
    for i in result["skipped"]:
        print(f"kept (not majority-refuted): {i}")

    had_failure = any(n.startswith("FAILED") for r in result["reverted"] for n in r["notes"])

    if write:
        import yaml

        try:
            yaml.safe_load(result["text"])
        except yaml.YAMLError as e:
            print(f"error: refusing to write, result does not parse as YAML: {e}", file=sys.stderr)
            return 1
        backup_out = paths.stage_dir(run, "i5") / "STATUS.pre-revert.yaml"
        backup_out.write_text(status_path.read_text(encoding="utf-8"), encoding="utf-8")
        status_path.write_text(result["text"], encoding="utf-8")
        print(f"STATUS.yaml written (previous version backed up to {backup_out})")
    else:
        print("dry run: nothing written")
    return 1 if had_failure else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
