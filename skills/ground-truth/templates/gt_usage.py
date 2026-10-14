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
from datetime import date, timedelta
import json
from pathlib import Path
import sys

import yaml

import gt_pending
import gt_store

LOCAL = "ground-truth-usage.json"
LOCK = "ground-truth-usage.lock"
COMMITTED = f"{gt_store.DIR}/usage.yaml"
UNUSED_DAYS = 90
PER_BATCH = 5  # unused candidates queued by one batch review


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


def committed(root):
    path = Path(root) / COMMITTED
    data = yaml.safe_load(path.read_text()) if path.is_file() else None
    return data if isinstance(data, dict) else {}


def fold(root, keep):
    """Merge the local record and this month for the `keep` ids into usage.yaml; returns what it holds."""
    local, old = _local(root), committed(root)
    month = gt_pending._today().isoformat()[:7]
    claims = {str(k): str(v) for k, v in (old.get("claims") or {}).items()}
    for cid, day in [*(local.get("last") or {}).items(), *((cid, month) for cid in keep)]:
        claims[cid] = max(claims.get(cid, ""), day[:7])
    since = [str(s)[:7] for s in (old.get("since"), local.get("since")) if s]
    if not (since or claims):
        return old
    new = {"since": min(since or [month]), "claims": dict(sorted(claims.items()))}
    if new != old:
        (Path(root) / gt_store.DIR).mkdir(exist_ok=True)
        gt_pending._write(Path(root) / COMMITTED, yaml.safe_dump(new, sort_keys=False))
    return new


def _month_end(month):
    y, m = map(int, month.split("-"))
    return date(y + m // 12, m % 12 + 1, 1) - timedelta(days=1)


def unused(root, data, usage):
    """(last use, claim id) of the claims due for review, oldest first; last use is None when never read.
    A committed month counts as its last day, so a claim is never proposed early."""
    local = _local(root)
    since = [date.fromisoformat(local["since"])] if local.get("since") else []
    since += [_month_end(str(usage["since"]))] if usage.get("since") else []
    days = ((data.get("meta") or {}).get("ttl") or {}).get("unused_days", UNUSED_DAYS)
    cutoff = gt_pending._today() - timedelta(days=days)
    if not since or min(since) >= cutoff:
        return []
    exempt = {d.get("ref") for d in data.get("debt") or [] if isinstance(d, dict) and d.get("state") == "open"}
    exempt |= {e.get(k) for e in data.get("edges") or [] if isinstance(e, dict) for k in ("from", "to")}
    for rel in gt_pending._git(root, "ls-files", "*.md").splitlines():
        if (Path(root) / rel).is_file():
            exempt.update(gt_store.CLAIM_REF_RE.findall((Path(root) / rel).read_text(errors="replace")))
    out = []
    for claim in data.get("claims") or []:
        cid = claim.get("id") if isinstance(claim, dict) else None
        if (not isinstance(cid, str) or cid in exempt or claim.get("canary") or claim.get("mutation")
                or claim.get("kind") == "live_state"):
            continue
        last = [date.fromisoformat(local["last"][cid])] if cid in (local.get("last") or {}) else []
        last += [_month_end(str(usage["claims"][cid]))] if cid in (usage.get("claims") or {}) else []
        if not last or max(last) < cutoff:
            out.append((max(last) if last else None, cid))
    return sorted(out, key=lambda item: (item[0] or date.min, item[1]))


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
