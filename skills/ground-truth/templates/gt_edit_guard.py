#!/usr/bin/env python3
"""PreToolUse gate: three checks fire in order before an edit is let
through -- a probe under tools/ground_truth/probes/ that is green, past
a small rewrite budget without the owner's sign-off; a file a claim
already covers, until this session has read its blast radius; a file
under the coverage roots that no claim covers (advisory or blocking,
per meta.enforcement). Only the last check bends to meta.enforcement --
the probe budget and the blast gate are hard regardless of it.

Reads the hook JSON on stdin, writes nothing on stdout. Exit 0 lets the
edit through; exit 2 denies it and hands the reason back to the session.

Bash is in scope too: a command whose text carries a write shape (`>`,
`sed -i`, `tee`, `cp`, `mv`, `rm`, a heredoc, ...) and names a path under
the coverage roots runs through the same gate as an Edit or Write to
that path. A read-only Bash command never touches STATUS.yaml at all --
the write-shape check is a plain regex over the command string, checked
first and before anything else. Everything that actually proves a
change stays the Stop hook's job, where the evidence is git state and
no tool can route around it.

Path resolution trusts the session's cwd, not the command's own shell
state: an embedded `cd app && cat > billing.py <<EOF` reports the
candidate as `app`, not `app/billing.py`, since the `cd` itself is never
parsed. The token scan also can't tell a write target from an unrelated
path earlier in the line, so `git diff app/billing.py > /tmp/out.diff`
treats app/billing.py as written to -- a false positive, and a safe one,
since it only ever gates a command that never needed it.

Everything on the path from stdin to a verdict has to stay cheap: the hook
fires before every single edit. The parsed shape of STATUS.yaml is cached
next to the repository's git metadata, keyed by the file's mtime and size,
so the yaml parser is imported only when the contract itself changed.
"""
from __future__ import annotations

import fnmatch
import json
import re
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

CACHE_NAME = "ground-truth-paths-v2.json"
PROBES_PREFIX = "tools/ground_truth/probes/"
PROBE_TIMEOUT = 20

_TEST_SUFFIXES = (
    "_test.py",
    "_test.go",
    ".test.js",
    ".test.ts",
    ".spec.js",
    ".spec.ts",
)


def is_test_file(rel: str) -> bool:
    """True for a path that is itself a test.

    A test carries no claim of its own -- it is what a claim points at --
    so neither this guard nor the Stop enforcer's new-definition rule
    treats one as uncovered code. Both guards share this definition; it
    lives here because this file is the latency-sensitive one and must not
    pull in the verifier's yaml stack to answer the question.
    """
    posix = Path(rel).as_posix()
    parts = posix.split("/")
    if "tests" in parts[:-1]:
        return True
    name = parts[-1]
    if name.startswith("test_") and name.endswith(".py"):
        return True
    return name.endswith(_TEST_SUFFIXES)


def under_roots(rel: str, roots, excludes) -> bool:
    """At or below one of meta.coverage.roots, with no exclude glob hit.

    Same rule as the verifier's coverage scan, restated here rather than
    imported: importing contract_lib costs the yaml parser on every edit,
    which is most of this guard's latency budget.
    """
    inside = False
    for r in roots or []:
        if not isinstance(r, str):
            continue
        prefix = r.strip("/")
        if not prefix:
            continue
        if rel == prefix or rel.startswith(prefix + "/"):
            inside = True
            break
    if not inside:
        return False
    return not any(fnmatch.fnmatch(rel, p) for p in excludes or [] if isinstance(p, str))


def claim_ids_for(rel: str, entries) -> list[str]:
    """Ids of the claims whose path names this file, or a directory above it."""
    ids: list[str] = []
    for pair in entries or []:
        if not isinstance(pair, list) or len(pair) != 2:
            continue
        p, cid = pair
        if not isinstance(p, str) or not isinstance(cid, str):
            continue
        if (rel == p or rel.startswith(p.rstrip("/") + "/")) and cid not in ids:
            ids.append(cid)
    return ids


def _git(dirpath, args, timeout: int = 10):
    return subprocess.run(
        ["git", "-C", str(dirpath), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _locate(dirpath) -> tuple[Path | None, Path | None]:
    """(work tree root, git dir) for a directory inside a repository.

    --git-dir, not --git-common-dir: in a linked worktree the common dir is
    shared with every other worktree of the same repository, and each of
    them has its own STATUS.yaml -- one shared cache file would answer for
    the wrong contract.
    """
    try:
        cp = _git(dirpath, ["rev-parse", "--show-toplevel", "--git-dir"])
    except (OSError, subprocess.SubprocessError):
        return None, None
    if cp.returncode != 0:
        return None, None
    lines = cp.stdout.splitlines()
    if len(lines) < 2 or not lines[0].strip():
        return None, None
    git_dir = Path(lines[1].strip())
    if not git_dir.is_absolute():
        git_dir = Path(dirpath) / git_dir
    return Path(lines[0].strip()), git_dir


def _read_cache(cache_path: Path, st) -> dict | None:
    try:
        cfg = json.loads(cache_path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(cfg, dict):
        return None
    if cfg.get("mtime_ns") != st.st_mtime_ns or cfg.get("size") != st.st_size:
        return None
    return cfg


def _build_cache(status_path: Path, st) -> dict:
    # Imported here and not at module level: on a cache hit the parser is
    # never needed, and its import is the single largest cost in this file.
    import yaml

    data = yaml.safe_load(status_path.read_text()) or {}
    if not isinstance(data, dict):
        data = {}
    meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
    coverage = meta.get("coverage") if isinstance(meta.get("coverage"), dict) else {}
    paths: list[list[str]] = []
    for claim in data.get("claims") or []:
        if not isinstance(claim, dict):
            continue
        cid = claim.get("id")
        if not isinstance(cid, str) or not cid:
            continue
        raw = claim.get("path")
        entries = [raw] if isinstance(raw, str) else raw
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if isinstance(entry, str) and entry:
                paths.append([Path(os.path.normpath(entry)).as_posix(), cid])
    return {
        "mtime_ns": st.st_mtime_ns,
        "size": st.st_size,
        "roots": [r for r in coverage.get("roots") or [] if isinstance(r, str)],
        "exclude": [e for e in coverage.get("exclude") or [] if isinstance(e, str)],
        "enforcement": meta.get("enforcement", "advisory"),
        "paths": paths,
    }


def _store_cache(cache_path: Path, cfg: dict) -> None:
    # A torn cache file would be rejected by the mtime/size check on the
    # next read anyway, but two hooks writing at once is common enough to
    # be worth the rename.
    tmp = cache_path.with_name(f"{cache_path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(cfg))
        os.replace(tmp, cache_path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


def _status_dirty(root: Path) -> bool:
    try:
        cp = _git(root, ["status", "--porcelain", "--", "STATUS.yaml"])
    except (OSError, subprocess.SubprocessError):
        return True
    return bool(cp.stdout.strip())


def _blast_gate(git_dir: Path, rel: str, sid: str, ids: list[str], via: str = "") -> int:
    try:
        import gt_hook_state
    except Exception:
        return 0
    if gt_hook_state.blast_ok(git_dir, sid, rel):
        return 0
    py = sys.executable
    suffix = f" ({via})" if via else ""
    print(
        f"ground-truth: {rel}{suffix} is covered by claim(s) {', '.join(ids)} -- "
        "read its blast radius before editing:\n"
        f"  {py} tools/ground_truth/blast_radius.py {rel}\n"
        "then retry the edit.",
        file=sys.stderr,
    )
    return 2


def _probe_rule(root: Path, git_dir: Path, rel: str, sid: str, via: str = "") -> int:
    target = root / rel
    if not target.is_file():
        return 0
    try:
        cp = subprocess.run(
            [sys.executable, str(target)],
            cwd=root,
            capture_output=True,
            timeout=PROBE_TIMEOUT,
        )
    except Exception:
        # Timeout, a missing interpreter, anything -- the probe is red.
        return 0
    if cp.returncode != 0:
        return 0

    try:
        import gt_hook_state
    except Exception:
        return 0

    rewrites = gt_hook_state.probe_rewrites(git_dir, sid)
    if rel in rewrites:
        return 0
    budget = int(os.environ.get("GT_PROBE_BUDGET", "2"))
    n = len(rewrites)
    if n < budget:
        gt_hook_state.probe_rewrite(git_dir, sid, rel)
        return 0

    used = ", ".join(sorted(rewrites))
    suffix = f" ({via})" if via else ""
    print(
        f"ground-truth: {rel}{suffix} is green; rewriting a green probe changes the "
        f"contract -- {n} of {budget} allowed this session already used "
        f"({used}).\n"
        f'Stop and report to the owner: "ESCALATE: contract change -- '
        f'{n + 1} green probes need rewriting, confirm".\n'
        "Owner override: GT_PROBE_BUDGET=<n> in the environment.",
        file=sys.stderr,
    )
    return 2


# Bash write shapes worth gating: redirects, in-place sed/perl, tee,
# cp/mv/rm (git mv/rm included -- "mv"/"rm" match as plain words), a
# heredoc, or open(path, "w"/"a"). A simple regex over the raw command
# text; cat/rg/ls and friends never match, so a read-only Bash command
# costs nothing beyond this check.
_BASH_WRITE_RE = re.compile(
    r">>|>|<<|\bsed\s+-[iI]|\btee\b|\bcp\b|\bmv\b|\brm\b|\bperl\s+-i|\btruncate\b|"
    r"open\([^()]*,\s*[\"'][wa]"
)
# Tokens split on whitespace and the shell separators | ; & -- good enough
# to pull candidate paths out of a command line without a real parser.
_BASH_TOKEN_RE = re.compile(r"[^\s|;&]+")


def _bash_candidate_rels(command: str, cwd: str, root: Path, cfg: dict) -> list[str]:
    """Command tokens that resolve under root and already exist there, or
    sit under a coverage root -- the rest is redirect syntax, flags and
    heredoc-body noise, none of which is a path this guard should gate."""
    root_r = root.resolve()
    rels: dict[str, None] = {}
    for raw in _BASH_TOKEN_RE.findall(command):
        token = raw.strip("'\"")
        if not token or token in (">", ">>", "<<"):
            continue
        p = Path(token)
        if not p.is_absolute():
            p = Path(cwd) / token
        try:
            rel = p.resolve().relative_to(root_r).as_posix()
        except (ValueError, OSError):
            continue
        if rel in rels:
            continue
        if (root / rel).is_file() or under_roots(rel, cfg.get("roots"), cfg.get("exclude")):
            rels[rel] = None
    return list(rels)


def _leading_checks(root: Path, git_dir: Path, rel: str, sid: str, via: str) -> int | None:
    """Verdicts that never need the coverage cache. None means "keep going"."""
    # The contract's own artifacts, human docs and tests are never claim
    # subjects; a claim about them would have nothing to point at. A probe
    # is the one exception under tools/ground_truth/: it carries its own
    # rule below, everything else there stays exempt.
    if rel == "STATUS.yaml" or rel.startswith("docs/"):
        return 0
    if rel.startswith("tools/ground_truth/"):
        if rel.startswith(PROBES_PREFIX) and rel.endswith(".py"):
            return _probe_rule(root, git_dir, rel, sid, via)
        return 0
    if is_test_file(rel):
        return 0
    return None


def _gate_covered_or_roots(root: Path, git_dir: Path, rel: str, sid: str, cfg: dict, via: str) -> int:
    # Claim coverage is answered from the cache, before git is asked
    # anything: the common case is an edit to a file that is already
    # claimed, and it should cost no subprocess at all. The blast gate
    # fires for a claimed path regardless of meta.coverage.roots -- a
    # claim can name a file the roots filter would otherwise skip.
    ids = claim_ids_for(rel, cfg.get("paths"))
    if ids:
        return _blast_gate(git_dir, rel, sid, ids, via)
    if not under_roots(rel, cfg.get("roots"), cfg.get("exclude")):
        return 0
    # STATUS.yaml already modified this session: the claim is being written
    # right now, and blocking here would only get in the way of writing it.
    if _status_dirty(root):
        return 0

    suffix = f" ({via})" if via else ""
    message = (
        f"ground-truth: {rel}{suffix} is under coverage roots and no claim covers it "
        "-- add a claim to STATUS.yaml first (rg -n path: recipe in "
        ".claude/rules/ground-truth.md), then edit"  # gt-allow: real rule path, not prose
    )
    if cfg.get("enforcement") == "blocking":
        print(message, file=sys.stderr)
        return 2
    print(f"(advisory) {message}", file=sys.stderr)
    return 0


def _check_path(root: Path, git_dir: Path, rel: str, sid: str, cfg: dict, via: str = "") -> int:
    rc = _leading_checks(root, git_dir, rel, sid, via)
    if rc is not None:
        return rc
    return _gate_covered_or_roots(root, git_dir, rel, sid, cfg, via)


def _run_bash(payload: dict, tool_input: dict, cwd: str) -> int:
    command = tool_input.get("command")
    if not isinstance(command, str) or not command or not _BASH_WRITE_RE.search(command):
        return 0

    root, git_dir = _locate(cwd)
    if root is None:
        return 0

    status_path = root / "STATUS.yaml"
    if not status_path.is_file() or not (root / "tools" / "ground_truth").is_dir():
        return 0

    try:
        st = status_path.stat()
    except OSError:
        return 0
    cache_path = git_dir / CACHE_NAME
    cfg = _read_cache(cache_path, st)
    if cfg is None:
        cfg = _build_cache(status_path, st)
        _store_cache(cache_path, cfg)

    sid = payload.get("session_id") or "unknown"
    for rel in _bash_candidate_rels(command, cwd, root, cfg):
        rc = _check_path(root, git_dir, rel, sid, cfg, "Bash edit")
        if rc:
            return rc
    return 0


def run() -> int:
    try:
        payload = json.load(sys.stdin)
    except (ValueError, OSError):
        return 0
    if not isinstance(payload, dict):
        return 0

    tool_input = payload.get("tool_input")
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    cwd = payload.get("cwd") or os.getcwd()

    if payload.get("tool_name") == "Bash":
        return _run_bash(payload, tool_input, cwd)

    file_path = tool_input.get("file_path")
    if not isinstance(file_path, str) or not file_path:
        return 0

    target = Path(file_path)
    if not target.is_absolute():
        target = Path(cwd) / target

    root, git_dir = _locate(cwd)
    if root is None:
        root, git_dir = _locate(target.parent)
    if root is None:
        return 0

    status_path = root / "STATUS.yaml"
    if not status_path.is_file() or not (root / "tools" / "ground_truth").is_dir():
        return 0

    try:
        rel = target.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return 0

    sid = payload.get("session_id") or "unknown"

    rc = _leading_checks(root, git_dir, rel, sid, "")
    if rc is not None:
        return rc

    try:
        st = status_path.stat()
    except OSError:
        return 0
    cache_path = git_dir / CACHE_NAME
    cfg = _read_cache(cache_path, st)
    if cfg is None:
        cfg = _build_cache(status_path, st)
        _store_cache(cache_path, cfg)

    return _gate_covered_or_roots(root, git_dir, rel, sid, cfg, "")


def main() -> int:
    try:
        return run()
    except Exception:
        # Fail open, and silently: this hook runs before every edit, and a
        # bug in it must never be the reason an edit cannot be made.
        return 0


if __name__ == "__main__":
    sys.exit(main())
