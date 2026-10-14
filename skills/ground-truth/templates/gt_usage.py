#!/usr/bin/env python3
"""When each contract claim was last read, for the review of claims nobody uses.

Usage: python3 tools/ground_truth/gt_usage.py --root DIR touch-session

The raw record is local, in the git common dir so every worktree of a
repository adds to the same one: ground-truth-usage.json =
{"since": "YYYY-MM-DD", "last": {claim id: "YYYY-MM-DD"}}. gt context
touches the claims it shows as facts, gt show the claims it prints, and the
Stop hook (touch-session) the claims whose path the session changed. verify
and batch never touch it: they read every claim.

gt batch folds it into the committed .ground-truth/usage.yaml
({since: YYYY-MM, claims: {id: YYYY-MM}}): the later month wins, keys are
sorted, and batch is the only writer. A claim not read for
meta.ttl.unused_days (90) days, with the record itself older than that,
becomes a candidate for the queue class unused:<id>, unless it is a canary,
has a mutation, is live_state, is named by open debt or an edge, or a
tracked .md file names it as STATUS.yaml#<id> or claim:<id>.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import gt_pending
import gt_store

LOCAL = "ground-truth-usage.json"
LOCK = "ground-truth-usage.lock"


def _local_path(root):
    common = gt_pending._git(root, "rev-parse", "--git-common-dir").strip()
    return Path(root) / common / LOCAL if common else None


def _local(root):
    path = _local_path(root)
    return json.loads(path.read_text()) if path and path.is_file() else {}


def touch(root, ids):
    """Record today as the last use of `ids`; nothing outside a git repository."""
    path = _local_path(root)
    if path is None or not ids:
        return
    today = gt_pending._today().isoformat()
    with gt_pending._hold(path.with_name(LOCK)):
        data = _local(root)
        data.setdefault("since", today)
        last = data.setdefault("last", {})
        for cid in ids:
            last[cid] = max(last.get(cid, today), today)
        gt_pending._write(path, json.dumps(data, sort_keys=True))


def touch_session(root):
    """Touch the claims whose path the working tree changes, the change set the session guard reads."""
    import contract_lib
    import gt_session_guard

    changed = gt_session_guard._change_set(contract_lib._git_toplevel(root), root, "hook", "HEAD")
    touch(root, [c["id"] for c in gt_store.load(root).data.get("claims") or []
                 if isinstance(c, dict) and isinstance(c.get("id"), str) and contract_lib._claim_path_changed(c, changed)])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("command", choices=["touch-session"])
    args = parser.parse_args(argv)
    touch_session(args.root)
    return 0


if __name__ == "__main__":
    sys.exit(main())
