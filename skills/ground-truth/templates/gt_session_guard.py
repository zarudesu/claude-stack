#!/usr/bin/env python3
"""Prove, from state alone, that a change ships what it claims.

Usage: python3 tools/ground_truth/gt_session_guard.py --mode hook|ci
           [--root DIR] [--base REF] [--max-checks N] [--budget-seconds S]

The verifier answers "does STATUS.yaml still describe this repository".
This answers the other half -- "did this particular change earn the status
it wrote down" -- from git and STATUS.yaml. Hooks run current checks and
defer mutation proof to isolated CI, leaving the user's files alone. Three rules:

  R1  every implemented/partial claim whose files or check targets moved
      in this change has its check run for real; red is a FAIL.
  R2  a claim that is new, or whose status was raised, ships a mutation
      that proves a green baseline, red mutation and restored green.
      No mutation description, no raise; no execution proof, no green CI.
  R3  a new top-level definition under the coverage roots is named by a
      test in this same change, or by a test a covering claim already
      points at. This is an advisory heuristic, not proof of coverage.

Exit 0 can include advisory warnings. Exit 1: a check is red or required
proof is missing. Hook mode reports checker errors and incomplete budgets
as warnings; CI fails closed on either. Defaults: hook 25 checks / 120s,
CI no count limit / 3600s, evaluated between checks. Individual checks
also have timeouts. Internal errors are logged in the git directory.
"""
from __future__ import annotations

import argparse
import os
import re
import signal
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    import contract_lib  # noqa: E402
    from gt_edit_guard import is_test_file  # noqa: E402
except Exception as exc:  # a half-installed tools/ground_truth is not a red check
    print(f"WARN guard internal error: {exc}", file=sys.stderr)
    sys.exit(1 if "ci" in sys.argv or "--mode=ci" in sys.argv else 0)

LOG_NAME = "ground-truth-guard.log"

_JS_DEFS = [
    re.compile(r"^(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+(\w+)"),
    re.compile(r"^(?:export\s+)?class\s+(\w+)"),
]
_DEF_PATTERNS = {
    ".py": [re.compile(r"^(?:async def|def|class)\s+(\w+)")],
    ".go": [re.compile(r"^func\s+(?:\(\w+\s+\*?\w+\)\s+)?(\w+)")],
    ".js": _JS_DEFS,
    ".jsx": _JS_DEFS,
    ".ts": _JS_DEFS,
    ".tsx": _JS_DEFS,
}


def _find_root(start: Path) -> Path:
    for candidate in [start, *start.parents]:
        if (candidate / "STATUS.yaml").is_file():
            return candidate
    return start


def _to_root(path: str, toplevel: Path, root: Path) -> str | None:
    """One git-reported path as the contract names it, or None when it sits
    outside root (a sibling component of the same repository)."""
    full = (toplevel.resolve() / os.path.normpath(path)).resolve()
    try:
        return full.relative_to(root.resolve()).as_posix()
    except ValueError:
        return None


def _resolve_base(toplevel: Path, mode: str, base_arg: str | None) -> tuple[str | None, str]:
    """(base ref, label). None means there is nothing to compare against and
    every rule here is silent -- an arbitrary base gives an arbitrary
    verdict, and this guard would rather say nothing."""
    if mode == "hook":
        return "HEAD", "HEAD"
    candidate = base_arg or os.environ.get("GT_BASE_REF", "").strip() or "HEAD~1"
    cp = contract_lib._git_run(toplevel, ["rev-parse", "--verify", f"{candidate}^{{commit}}"])
    if cp.returncode == 0:
        return candidate, candidate
    return None, candidate


def _change_set(toplevel: Path, root: Path, mode: str, base: str) -> set[str]:
    if mode == "hook":
        raw = contract_lib._porcelain_paths(toplevel)
        raw += contract_lib._diff_paths(toplevel, ["HEAD"])
    else:
        raw = contract_lib._diff_paths(toplevel, [base, "HEAD"])
    return contract_lib._rebase_to_root(raw, toplevel, root)


def _untracked_paths(toplevel: Path) -> list[str]:
    cp = contract_lib._git_run(toplevel, ["ls-files", "--others", "--exclude-standard", "-z"])
    if cp.returncode != 0:
        return []
    return [p for p in cp.stdout.split("\0") if p]


def _added_lines(toplevel: Path, root: Path, mode: str, base: str) -> dict[str, list[str]]:
    """Repo-relative path -> the lines this change added to it.

    An untracked file contributes its whole content: nothing of it existed
    before, so every line in it is an added line.
    """
    diff_args = ["-U0", "--no-color"] + (["HEAD"] if mode == "hook" else [base, "HEAD"])
    cp = contract_lib._git_run(toplevel, ["diff", *diff_args])
    out: dict[str, list[str]] = {}
    current: str | None = None
    if cp.returncode == 0:
        for line in cp.stdout.splitlines():
            if line.startswith("+++ "):
                target = line[4:].strip()
                if target == "/dev/null":
                    current = None
                else:
                    rel = _to_root(target[2:] if target.startswith("b/") else target, toplevel, root)
                    current = rel
                continue
            if line.startswith("--- ") or line.startswith("diff --git "):
                continue
            if current is not None and line.startswith("+"):
                out.setdefault(current, []).append(line[1:])

    if mode == "hook":
        for path in _untracked_paths(toplevel):
            rel = _to_root(path, toplevel, root)
            if rel is None:
                continue
            try:
                text = (root / rel).read_text(errors="ignore")
            except OSError:
                continue
            out.setdefault(rel, []).extend(text.splitlines())
    return out


def _baseline_claims(toplevel: Path, root: Path, base: str) -> dict[str, dict] | None:
    rel_status = contract_lib._rel_status_path(toplevel, root)
    if rel_status is None:
        raise ValueError("cannot locate baseline STATUS.yaml within the git repository")
    shown = contract_lib._git_run(toplevel, ["show", f"{base}:{rel_status}"])
    if shown.returncode != 0:
        listed = contract_lib._git_run(toplevel, ["ls-tree", "--name-only", base, "--", rel_status])
        if listed.returncode == 0 and not listed.stdout.strip():
            return {}  # First contract: every claim really is new.
        raise ValueError("cannot read baseline STATUS.yaml")
    baseline = contract_lib._baseline_status_claims(shown.stdout)
    if baseline is None:
        raise ValueError("baseline STATUS.yaml has unusable claims")
    return baseline


def _status_claims(data: dict) -> list[dict]:
    return [
        c
        for c in data.get("claims") or []
        if isinstance(c, dict) and c.get("kind") == "status" and isinstance(c.get("id"), str)
    ]


def _touches_change(claim: dict, changed: set[str]) -> bool:
    if contract_lib._claim_path_changed(claim, changed):
        return True
    kind = claim.get("check_kind")
    return any(
        contract_lib._target_changed(t, kind, changed) for t in contract_lib._check_targets(claim)
    )


def _claim_paths(claim: dict) -> list[str]:
    raw = claim.get("path")
    entries = [raw] if isinstance(raw, str) else raw
    if not isinstance(entries, list):
        return []
    return [Path(os.path.normpath(e)).as_posix() for e in entries if isinstance(e, str) and e]


def _check_command(root: Path, claim: dict) -> tuple[str, Path]:
    """(command line, cwd) this claim's check runs as -- for the fail
    recipe only, never executed here. Exact for pytest/probe, the check
    kinds this rule most often catches red; the rest fall back to a
    readable kind/check line rather than duplicating contract_lib's full
    per-kind argv construction."""
    kind = claim.get("check_kind")
    check = claim.get("check")
    parts = [p.strip() for p in check.split(";") if p.strip()] if isinstance(check, str) else []
    if kind == "pytest":
        return " ".join([sys.executable, "-m", "pytest", *parts, "-q", "-p", "no:cacheprovider"]), root
    if kind == "probe" and parts:
        full = root / parts[0]
        prog = str(full) if os.access(full, os.X_OK) else f"{sys.executable} {full}"
        return prog, root
    return f"{kind}: {check}", root


def _fail_recipe(root: Path, claim: dict, message: str) -> str:
    """claim id, the exact check command, the files it and its target
    cover, and a tail of what it printed -- so a red check names its own
    fix instead of leaving the session to guess at the same FAIL again."""
    cid = claim["id"]
    cmd, cwd = _check_command(root, claim)
    targets = list(dict.fromkeys(_claim_paths(claim) + contract_lib._check_targets(claim)))
    lines = [
        f"claim {cid}: check FAILED",
        f"  command: {cmd} (cwd={cwd})",
        f"  targets: {', '.join(targets) if targets else '-'}",
    ]
    lines += [f"  {line}" for line in message.splitlines()[-15:]]
    lines.append(
        f"  fix: make the check green (edit the code or the test it names), "
        f"or lower status: and add debt with ref: {cid}; never edit STATUS.yaml alone"
    )
    return "\n".join(lines)


class Budget:
    """How many real check runs this pass may still afford.

    One counter for both rules that run checks, not one each: the cost the
    cap exists to bound is wall-clock time on the Stop hook, and a check
    run costs the same whichever rule asked for it. A run cap alone does
    not bound that time -- 25 slow checks outlast any hook timeout -- so
    the seconds are counted too, and the first of the two to run out stops
    the remaining claims.
    """

    def __init__(self, limit: int | None, seconds: int) -> None:
        self.left = limit
        self.ran = 0
        self.not_run: list[str] = []
        self.deadline = time.monotonic() + seconds
        self.expired = False

    def take(self, cid: str) -> bool:
        if time.monotonic() >= self.deadline:
            self.expired = True
            self.not_run.append(cid)
            return False
        if self.left is not None and self.left <= 0:
            self.not_run.append(cid)
            return False
        if self.left is not None:
            self.left -= 1
        self.ran += 1
        return True


def rule_touched_claims(root, claims, changed, budget) -> list[tuple[str, str, str]]:
    """R1: a claim whose subject moved in this change has its check run."""
    issues = []
    for claim in claims:
        if claim.get("status") not in ("implemented", "partial"):
            continue
        cid = claim["id"]
        if not _touches_change(claim, changed):
            continue
        if not budget.take(cid):
            continue
        ok, message, skipped = contract_lib.run_claim_check(root, claim)
        if skipped:
            issues.append(("warn", cid, f"claim {cid}: check could not run here: {message}"))
        elif not ok:
            issues.append(("fail", cid, _fail_recipe(root, claim, message)))
    return issues


def _mutate_and_run(root: Path, claim: dict, mutation: dict) -> tuple[bool, str, bool]:
    """(went_red, message, skipped) with the mutation applied.

    The original bytes are restored whatever happens, and by rewriting the
    file rather than by git: in hook mode the file is usually dirty, and a
    checkout would throw away the session's own work.
    """
    baseline_ok, message, skipped = contract_lib.run_claim_check(root, claim)
    if not baseline_ok:
        return False, f"baseline check is not green: {message}", skipped
    rel_file = mutation["file"]
    full = root / rel_file
    original = full.read_bytes()
    text = original.decode("utf-8", errors="strict")
    if mutation["find"] not in text:
        return False, f"mutation.find not present in {rel_file}", False

    def restore() -> None:
        full.write_bytes(original)
        # Bytecode written against the mutated source outlives it when the
        # mutation and the restore land in the same second.
        cache = full.parent / "__pycache__"
        if cache.is_dir():
            for item in cache.glob("*.pyc"):
                try:
                    item.unlink()
                except OSError:
                    pass

    def on_signal(signum, _frame):
        # A finally alone does not survive a signal: the Stop hook's own
        # timeout arrives as SIGTERM, and the mutated file would stay on
        # disk as if the session had written it.
        restore()
        os._exit(128 + signum)

    previous = {}
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        try:
            previous[sig] = signal.signal(sig, on_signal)
        except (OSError, ValueError):
            pass
    full.write_bytes(text.replace(mutation["find"], mutation["replace"], 1).encode("utf-8"))
    try:
        ok, message, skipped = contract_lib.run_claim_check(root, claim)
    finally:
        restore()
        for sig, handler in previous.items():
            try:
                signal.signal(sig, handler)
            except (OSError, ValueError):
                pass
    if not ok and not skipped:
        restored_ok, restored_message, restored_skipped = contract_lib.run_claim_check(root, claim)
        if not restored_ok:
            return False, f"restored check is not green: {restored_message}", restored_skipped
    return (not ok and not skipped), message, skipped


def rule_new_claims(root, claims, baseline, budget, prove_mutations=True) -> list[tuple[str, str, str]]:
    """R2: a new or raised implemented/partial claim carries its red proof."""
    if baseline is None:
        return []
    issues = []
    for claim in claims:
        if claim.get("status") not in ("implemented", "partial"):
            continue
        cid = claim["id"]
        old = baseline.get(cid)
        if old is not None:
            old_rank = contract_lib._STATUS_RANK.get(old.get("status"))
            new_rank = contract_lib._STATUS_RANK.get(claim.get("status"))
            if old_rank is None or new_rank is None or new_rank <= old_rank:
                continue
        mutation = claim.get("mutation")
        fields = ("file", "find", "replace")
        if not isinstance(mutation, dict) or any(mutation.get(f) is None for f in fields):
            issues.append(
                (
                    "fail",
                    cid,
                    f"new/raised implemented claim {cid} needs mutation: "
                    "file/find/replace (authoring-time red proof)\n"
                    f"  fix: add mutation: {{file, find, replace}} to claim {cid} that turns its check red",
                )
            )
            continue
        if not prove_mutations:
            issues.append(("warn", cid, "mutation proof pending CI/isolated acceptance; hook did not mutate the working tree"))
            continue
        dirty = contract_lib._git_run(root, ["status", "--porcelain", "--", str(mutation["file"])])
        if dirty.returncode or dirty.stdout.strip():
            issues.append(("fail", cid, "mutation target is dirty or its state is unavailable; use an isolated committed tree"))
            continue
        if not budget.take(cid):
            continue
        try:
            went_red, message, skipped = _mutate_and_run(root, claim, mutation)
        except (OSError, UnicodeDecodeError) as exc:
            issues.append(("warn", cid, f"mutation of {cid} could not be applied: {exc}"))
            continue
        if skipped:
            issues.append(("warn", cid, f"mutation of {cid} not proven here: {message}"))
        elif not went_red:
            detail = f": {message}" if message else ""
            issues.append(
                (
                    "fail",
                    cid,
                    f"mutation of {cid} did not turn the check red{detail}\n"
                    f"  fix: change mutation.find/replace of {cid} so the check fails, or strengthen the check it names",
                )
            )
    return issues


def _new_definitions(rel: str, lines: list[str]) -> list[str]:
    patterns = _DEF_PATTERNS.get(Path(rel).suffix)
    if not patterns:
        return []
    names = []
    for line in lines:
        for pat in patterns:
            m = pat.match(line)
            if m:
                name = m.group(1)
                if not name.startswith("_") and not name.startswith("test"):
                    names.append(name)
                break
    return names


def rule_new_definitions(root, claims, changed, added, roots, excludes) -> list[tuple[str, str, str]]:
    """R3: new top-level code is named by a test somewhere in this change."""
    test_text = "\n".join(
        "\n".join(lines) for rel, lines in added.items() if is_test_file(rel)
    )
    issues = []
    target_cache: dict[str, str] = {}
    for rel in sorted(added):
        if rel not in changed:
            continue
        if rel == "STATUS.yaml" or rel.startswith("tools/ground_truth/"):
            continue
        if is_test_file(rel) or not contract_lib._under_coverage_roots(rel, roots, excludes):
            continue
        names = _new_definitions(rel, added[rel])
        if not names:
            continue

        covering = ""
        for claim in claims:
            if not any(rel == p or rel.startswith(p.rstrip("/") + "/") for p in _claim_paths(claim)):
                continue
            for target in contract_lib._check_targets(claim):
                if target not in target_cache:
                    try:
                        target_cache[target] = (root / target).read_text(errors="ignore")
                    except OSError:
                        target_cache[target] = ""
                covering += target_cache[target]

        for name in names:
            if name in test_text or name in covering:
                continue
            issues.append(
                (
                    "warn",
                    "-",
                    f"new definition {name} in {rel}: no test reference in this change "
                    "and none in a covering claim check\n"
                    "  review: confirm behavioral coverage; a literal symbol name is only a heuristic",
                )
            )
    return issues


def _log_traceback(root: Path) -> None:
    try:
        cp = contract_lib._git_run(root, ["rev-parse", "--git-dir"])
        git_dir = Path(cp.stdout.strip()) if cp.returncode == 0 and cp.stdout.strip() else Path(".git")
        if not git_dir.is_absolute():
            git_dir = root / git_dir
        with (git_dir / LOG_NAME).open("a") as fh:
            fh.write(traceback.format_exc())
    except Exception:
        # Nowhere to write the log is not a reason to fail the session.
        pass


def guard(args) -> int:
    root = args.root or _find_root(Path(__file__).resolve().parent)
    data = contract_lib.load_contract(root)
    toplevel = contract_lib._git_toplevel(root)
    issues: list[tuple[str, str, str]] = []

    base, label = _resolve_base(toplevel, args.mode, args.base)
    if base is None:
        issues.append(("fail" if args.mode == "ci" else "warn", "-",
                       f"base ref {label} does not resolve, rules skipped"))
        changed: set[str] = set()
        added: dict[str, list[str]] = {}
        baseline: dict[str, dict] | None = None
        claims: list[dict] = []
    else:
        claims = _status_claims(data)
        changed = _change_set(toplevel, root, args.mode, base)
        added = _added_lines(toplevel, root, args.mode, base)
        baseline = _baseline_claims(toplevel, root, base)

    meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
    coverage = meta.get("coverage") if isinstance(meta.get("coverage"), dict) else {}
    budget = Budget(args.max_checks, args.budget_seconds)

    # R2 spends the budget first: a missing red proof is the one thing
    # nothing else in the chain ever catches, while R1's checks are also
    # run by CI on the merged result.
    proof_issues = rule_new_claims(root, claims, baseline, budget, prove_mutations=args.mode == "ci")
    proof_issues += rule_touched_claims(root, claims, changed, budget)
    # An unavailable required check is an incomplete CI run, not a success.
    issues += [("fail" if args.mode == "ci" and severity == "warn" else severity, cid, message)
               for severity, cid, message in proof_issues]
    issues += rule_new_definitions(
        root, claims, changed, added, coverage.get("roots") or [], coverage.get("exclude") or []
    )
    if budget.not_run:
        spent = (
            f"time budget of {args.budget_seconds}s exhausted"
            if budget.expired
            else f"check budget of {args.max_checks} reached"
        )
        issues.append(
            (
                "fail" if args.mode == "ci" else "warn",
                "-",
                f"{spent}, not run: " + ", ".join(budget.not_run),
            )
        )

    fails = sum(1 for severity, _, _ in issues if severity == "fail")
    warns = len(issues) - fails
    for severity, cid, message in issues:
        print(f"{severity.upper()} [{cid}] {message}")
    ran = budget.ran
    print(
        f"session guard: mode={args.mode} base={label} checks_run={ran} "
        f"fail={fails} warn={warns}"
    )
    return 1 if fails else 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=None)
    parser.add_argument("--mode", choices=["hook", "ci"], required=True)
    parser.add_argument("--base", default=None, help="ref this change is measured against (ci mode)")
    parser.add_argument("--max-checks", type=int, default=None)
    parser.add_argument("--budget-seconds", type=int, default=None,
                        help="wall-clock seconds this pass may spend running checks")
    args = parser.parse_args(argv)
    if args.max_checks is None and args.mode == "hook":
        args.max_checks = 25
    if args.budget_seconds is None:
        args.budget_seconds = 3600 if args.mode == "ci" else 120
    try:
        return guard(args)
    except Exception as exc:
        print(f"WARN guard internal error: {exc}", file=sys.stderr)
        _log_traceback(args.root or Path.cwd())
        return 1 if args.mode == "ci" else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
