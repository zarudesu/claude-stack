#!/usr/bin/env python3
"""Decide whether an `audit` run short-circuits or goes scoped.

Compare code, check targets and claim definitions with the last substantive
review (meta.last_audited_commit), including dirty and untracked files.
A passing, recent, unchanged contract needs no model workflow or timestamp
refresh. Otherwise report a scope for review, not an automatic agent fleet.
A seeded sample is a review aid, not proof that untouched claims are correct.

Writes run.scratch/audit/audit-scope.json; never writes into the repo.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import subprocess
import sys
from datetime import date
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from gt_lib.git import _run  # noqa: E402
from gt_lib.paths import load_run, stage_dir  # noqa: E402
from stages.i9_install import drift_report, gather_facts  # noqa: E402


def _under(path: str, roots: list[str]) -> bool:
    normalized = [str(Path(r)).rstrip("/") for r in roots]
    return any(r in ("", ".") or path == r or path.startswith(r + "/") for r in normalized)


def audit_base(repo: Path, meta: dict) -> tuple[str | None, str]:
    """A routine sync is not a semantic audit checkpoint."""
    candidate = meta.get("last_audited_commit")
    if not candidate:
        return None, "no recorded semantic audit commit"
    try:
        base = _run(repo, ["rev-parse", "--verify", f"{candidate}^{{commit}}"]).strip()
        if base and subprocess.run(["git", "merge-base", "--is-ancestor", base, "HEAD"],
                                   cwd=repo, capture_output=True).returncode == 0:
            return base, ""
    except (OSError, RuntimeError):
        pass
    return None, "recorded audit commit is unavailable or not an ancestor of HEAD"


def drift_files(repo: Path, base: str | None, roots: list[str]) -> list[str]:
    files: set[str] = set()
    if base:
        files.update(_run(repo, ["diff", "--name-only", "--no-renames", "-z", base, "HEAD"]).split("\0"))
    else:
        files.update(_run(repo, ["ls-files", "-z"]).split("\0"))
    files.update(_run(repo, ["diff", "--name-only", "--no-renames", "-z", "HEAD"]).split("\0"))
    files.update(_run(repo, ["ls-files", "--others", "--exclude-standard", "-z"]).split("\0"))
    files.discard("")
    return sorted(p for p in files if _under(p, roots))


def verify(run, mode: str = "full") -> dict:
    proc = subprocess.run([str(run.python), "tools/ground_truth/verify.py", f"--mode={mode}"],
                          cwd=run.repo, capture_output=True, text=True)
    lines = (proc.stdout + proc.stderr).splitlines()
    return {"exit": proc.returncode,
            "fail": sum(l.startswith("[FAIL]") for l in lines),
            "warn": sum(l.startswith("[WARN]") for l in lines),
            "fail_ids": sorted({l.split()[1] for l in lines if l.startswith("[FAIL]") and len(l.split()) > 1})}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run", required=True, help="path to run.json")
    parser.add_argument("--status", help="path to STATUS.yaml (default: run.repo/STATUS.yaml)")
    parser.add_argument("--max-age-days", type=int, default=30)
    parser.add_argument("--sample", type=int, default=5, help="untouched claims to re-verify in scoped mode")
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--skip-verify", action="store_true", help="inspect scope without proof; short-circuit is disabled")
    args = parser.parse_args(argv)

    run = load_run(args.run)
    status_path = Path(args.status) if args.status else run.repo / "STATUS.yaml"
    if not status_path.is_file():
        print(f"error: STATUS.yaml not found at {status_path}; run ground-truth init first", file=sys.stderr)
        return 1
    data = yaml.safe_load(status_path.read_text(encoding="utf-8")) or {}
    meta = data.get("meta", {})
    roots = [r.rstrip("/") for r in meta.get("coverage", {}).get("roots", [])]
    rel_status = str(status_path.resolve().relative_to(run.repo.resolve()))

    base, base_reason = audit_base(run.repo, meta)
    head = _run(run.repo, ["rev-parse", "HEAD"]).strip()
    all_drift = drift_files(run.repo, base, ["."])
    drift = [p for p in all_drift if _under(p, roots)]

    claims = data.get("claims", []) or []
    try:
        previous = yaml.safe_load(_run(run.repo, ["show", f"{base}:{rel_status}"])) if base else {}
    except (RuntimeError, yaml.YAMLError):
        previous = {}
    previous = previous or {}
    old_claims = {c["id"]: c for c in previous.get("claims", []) or []}
    changed_claims = [c["id"] for c in claims if old_claims.get(c["id"]) != c]
    removed_claims = sorted(set(old_claims) - {c["id"] for c in claims})
    coverage_changed = previous.get("meta", {}).get("coverage") != meta.get("coverage")
    links_or_debt_changed = any(previous.get(key) != data.get(key) for key in ("edges", "debt"))
    touched, untouched = [], []
    for c in claims:
        paths = c.get("path") or []
        paths = [paths] if isinstance(paths, str) else list(paths)
        for check in str(c.get("check") or "").split(";"):
            target = check.strip().split("::", 1)[0]
            if target and c.get("check_kind") != "junit":
                paths.append(target)
        hit = [p for p in paths if p in all_drift or any(d.startswith(p.rstrip("/") + "/") for d in all_drift)]
        if c.get("check_kind") == "junit":
            hit += [d for d in all_drift for check in str(c.get("check") or "").split(";")
                    if ("/" + d).endswith("/src/test/java/" + check.strip().split("::", 1)[0].replace(".", "/") + ".java")]
        (touched if hit or c["id"] in changed_claims else untouched).append(c["id"])
    seed = args.seed if args.seed is not None else int(hashlib.sha256(f"{head}:{run.date}".encode()).hexdigest()[:16], 16)
    rng = random.Random(seed)
    sample = sorted(rng.sample(untouched, min(args.sample, len(untouched))))

    la = str(meta.get("last_audited", ""))
    try:
        age = (date.fromisoformat(run.date) - date.fromisoformat(la)).days
    except ValueError:
        age = None
    ver = None if args.skip_verify else verify(run)
    short = bool(base and ver and ver["exit"] == 0 and not drift and not touched
                 and not removed_claims and not coverage_changed and not links_or_debt_changed
                 and age is not None and 0 <= age <= args.max_age_days)

    facts = gather_facts(run.repo)
    template_drift = drift_report(run.repo, facts, run.skill / "templates")
    stale = [d["path"] for d in template_drift if d["status"] != "same"]

    next_steps = (["Report executed checks and warnings; no model workflow or audit timestamp update needed"]
                  if short else
                  ["Review drift_files/touched_claims and sample_claims as needed; no automatic full pipeline",
                   "After substantive review and authorized fixes: audit_close --reviewed --write"])
    navigation_changed = any(p in ("CLAUDE.md", "AGENTS.md", ".claude/rules/ground-truth.md",
                                     "tools/ground_truth/blast_radius.py") for p in all_drift)
    if navigation_changed:
        next_steps.append("Navigation changed: spot-check an affected route; a fleet is optional")
    # Template drift is a skill-update signal, not a repo fact -- it never
    # flips short_circuit, only appends an action item to next.
    if stale:
        next_steps = next_steps + [f"Inspect i9_install dry-run/diff before updating {len(stale)} outdated/missing files: "
                                    + ", ".join(stale)]

    out = {
        "repo": str(run.repo), "date": run.date, "base": base, "base_reason": base_reason, "head": head,
        "last_audited": la, "age_days": age, "max_age_days": args.max_age_days,
        "verify": ver, "roots": roots, "drift_files": drift,
        "touched_claims": touched, "changed_claims": changed_claims, "removed_claims": removed_claims,
        "coverage_changed": coverage_changed, "sample_claims": [] if short else sample, "sample_seed": seed,
        "links_or_debt_changed": links_or_debt_changed,
        "navigation_changed": navigation_changed,
        "template_drift": template_drift,
        "short_circuit": short,
        "next": next_steps,
    }
    path = stage_dir(run, "audit") / "audit-scope.json"
    path.write_text(json.dumps(out, indent=1, ensure_ascii=False), encoding="utf-8")
    v = f"verify exit={ver['exit']} fail={ver['fail']} warn={ver['warn']}" if ver else "verify skipped"
    print(f"base={base[:7] if base else None} head={head[:7]} {v} last_audited={la} age={age}d "
          f"drift={len(drift)} touched={len(touched)}/{len(claims)} sample={len(sample)} "
          f"template_drift={len(stale)}/{len(template_drift)}")
    print("SHORT-CIRCUIT" if short else "SCOPED", "->", path)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
