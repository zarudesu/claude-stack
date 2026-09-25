#!/usr/bin/env python3
"""Per-worktree state for the ground-truth hooks: which files have had
their blast radius read this session, how many Stop attempts in a row
left STATUS.yaml red, and which green probes this session has already
rewritten.

One JSON file per worktree, next to the repository's own git metadata
(--git-dir, not --git-common-dir -- a linked worktree does not share its
STATUS.yaml with any other worktree of the same repository, so it must
not share this file either). Written atomically (tmp file, then
os.replace), pruned of sessions older than 7 days on every write.

Every function here fails open: an internal error never raises into the
caller, and answers True/0 rather than block a hook on a bug in the state
file itself. Import these by path from a sibling script
(sys.path.insert(0, dirname(__file__))); the same operations are also
reachable from the command line, kebab-cased, for the shell hooks that
cannot import a Python module directly.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

STATE_NAME = "ground-truth-state.json"
SEVEN_DAYS = 7 * 24 * 3600
FOUR_HOURS = 4 * 3600


def locate_git_dir(start: Path) -> Path | None:
    """--git-dir for the repository containing `start`, made absolute."""
    try:
        cp = subprocess.run(
            ["git", "-C", str(start), "rev-parse", "--git-dir"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if cp.returncode != 0:
        return None
    out = cp.stdout.strip()
    if not out:
        return None
    git_dir = Path(out)
    if not git_dir.is_absolute():
        git_dir = Path(start) / git_dir
    return git_dir


def _state_path(git_dir) -> Path:
    return Path(git_dir) / STATE_NAME


def _load(git_dir) -> dict:
    try:
        raw = json.loads(_state_path(git_dir).read_text())
    except (OSError, ValueError):
        raw = None
    if not isinstance(raw, dict):
        raw = {}
    sessions = raw.get("sessions")
    blast = raw.get("blast")
    return {
        "sessions": sessions if isinstance(sessions, dict) else {},
        "blast": blast if isinstance(blast, dict) else {},
    }


def _save(git_dir, state: dict) -> None:
    cutoff = time.time() - SEVEN_DAYS
    sessions = state.get("sessions") or {}
    for sid in [s for s, rec in sessions.items() if not isinstance(rec, dict) or rec.get("started", 0) < cutoff]:
        sessions.pop(sid, None)
    blast = state.get("blast") or {}
    for rel in [r for r, ts in blast.items() if not isinstance(ts, (int, float)) or ts < cutoff]:
        blast.pop(rel, None)
    path = _state_path(git_dir)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(state))
    os.replace(tmp, path)


def _writable(git_dir) -> bool:
    """True if an ack has somewhere to land. Probed by doing the same
    tmp-write plus os.replace an ack needs, never by a mode check on the
    state file alone: a read-only git-dir (chmod a-w, a full disk) still
    holds a writable-looking file, and blast_ok must not deny an edit
    the session has no way to ack. Only runs on the deny path."""
    try:
        _save(git_dir, _load(git_dir))
        return True
    except Exception:
        return False


def session_start(git_dir, sid: str) -> None:
    """Overwrite started = now. A restart after compaction resets acks on
    purpose: blast_ok compares an ack's timestamp against this value, so
    bumping it forward invalidates every ack recorded before the restart."""
    if not sid:
        return
    try:
        state = _load(git_dir)
        rec = state["sessions"].get(sid)
        rec = rec if isinstance(rec, dict) else {"stop_red": 0, "probe_rewrites": []}
        rec["started"] = time.time()
        rec.setdefault("stop_red", 0)
        rec.setdefault("probe_rewrites", [])
        state["sessions"][sid] = rec
        _save(git_dir, state)
    except Exception:
        pass


def blast_ack(git_dir, rels: list[str]) -> None:
    try:
        state = _load(git_dir)
        now = time.time()
        for rel in rels:
            state["blast"][rel] = now
        _save(git_dir, state)
    except Exception as exc:
        print(f"ground-truth: state not recorded ({exc}); the edit gate fails open", file=sys.stderr)


def blast_ok(git_dir, sid: str, rel: str) -> bool:
    try:
        state = _load(git_dir)
        ts = state["blast"].get(rel)
        if not isinstance(ts, (int, float)):
            # No ack for the exact path -- accept the newest ack recorded
            # against a directory that contains it (a directory-scoped
            # claim is acked by its claim id, not by each file under it).
            prefixed = [
                v for k, v in state["blast"].items()
                if isinstance(v, (int, float)) and rel.startswith(k.rstrip("/") + "/")
            ]
            ts = max(prefixed) if prefixed else None
        if not isinstance(ts, (int, float)):
            # No ack recorded, but if the state file has nowhere writable
            # to put one, denying the edit would be a permanent, silent
            # lock -- fail open instead.
            return not _writable(git_dir)
        rec = state["sessions"].get(sid)
        if isinstance(rec, dict) and isinstance(rec.get("started"), (int, float)):
            return ts >= rec["started"]
        return ts >= time.time() - FOUR_HOURS
    except Exception:
        return True


def stop_red(git_dir, sid: str) -> int:
    if not sid:
        return 0
    try:
        state = _load(git_dir)
        rec = state["sessions"].get(sid)
        rec = rec if isinstance(rec, dict) else {"started": int(time.time()), "probe_rewrites": []}
        rec["stop_red"] = int(rec.get("stop_red") or 0) + 1
        state["sessions"][sid] = rec
        _save(git_dir, state)
        return rec["stop_red"]
    except Exception:
        return 0


def stop_green(git_dir, sid: str) -> None:
    try:
        state = _load(git_dir)
        rec = state["sessions"].get(sid)
        rec = rec if isinstance(rec, dict) else {"started": int(time.time()), "probe_rewrites": []}
        rec["stop_red"] = 0
        state["sessions"][sid] = rec
        _save(git_dir, state)
    except Exception:
        pass


def probe_rewrites(git_dir, sid: str) -> list[str]:
    """Green probes this session has already rewritten (probe_count is
    just this list's length -- kept separate so a caller can also check
    whether one particular probe is already in the set)."""
    try:
        state = _load(git_dir)
        rec = state["sessions"].get(sid)
        rewrites = rec.get("probe_rewrites") if isinstance(rec, dict) else None
        return list(rewrites) if isinstance(rewrites, list) else []
    except Exception:
        return []


def probe_count(git_dir, sid: str) -> int:
    return len(probe_rewrites(git_dir, sid))


def probe_rewrite(git_dir, sid: str, rel: str) -> int:
    try:
        state = _load(git_dir)
        rec = state["sessions"].get(sid)
        rec = rec if isinstance(rec, dict) else {"started": int(time.time()), "stop_red": 0, "probe_rewrites": []}
        rewrites = rec.get("probe_rewrites")
        rewrites = rewrites if isinstance(rewrites, list) else []
        if rel not in rewrites:
            rewrites.append(rel)
        rec["probe_rewrites"] = rewrites
        state["sessions"][sid] = rec
        _save(git_dir, state)
        return len(rewrites)
    except Exception:
        return 0


def _parse_argv(argv: list[str]) -> tuple[str, list[str]]:
    args = list(argv)
    root = "."
    if "--root" in args:
        i = args.index("--root")
        root = args[i + 1]
        del args[i : i + 2]
    return root, args


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    try:
        root, args = _parse_argv(argv)
        if not args:
            return 0
        cmd, rest = args[0], args[1:]
        git_dir = locate_git_dir(Path(root))
    except Exception:
        return 0

    try:
        if cmd == "session-start":
            session_start(git_dir, rest[0])
            return 0
        if cmd == "blast-ack":
            blast_ack(git_dir, rest)
            return 0
        if cmd == "blast-ok":
            return 0 if blast_ok(git_dir, rest[0], rest[1]) else 1
        if cmd == "stop-red":
            print(stop_red(git_dir, rest[0]))
            return 0
        if cmd == "stop-green":
            stop_green(git_dir, rest[0])
            return 0
        if cmd == "probe-rewrite":
            print(probe_rewrite(git_dir, rest[0], rest[1]))
            return 0
        if cmd == "probe-count":
            print(probe_count(git_dir, rest[0]))
            return 0
    except Exception:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
