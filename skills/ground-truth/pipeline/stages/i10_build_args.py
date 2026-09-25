#!/usr/bin/env python3
"""Build the args file for the I.10 fleet-probe (acceptance) workflow.

Deterministically (under --seed) picks --n files covered by STATUS.yaml
status claims, spread across the coverage roots and preferring files
that carry two or more claims, then turns each into a fleet-probe
target: {x, expect}. x is a "where would you change ..." question built
from the chosen claim's component and note. expect names that claim's
id plus, for every sibling claim on the same file, its id and a one-line
why it is not the answer.

Writes run.scratch/i10/i10-args.json = {repo, python, targets, models?}.
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from gt_lib.paths import args_path, load_run  # noqa: E402


def _path_list(claim: dict) -> list[str]:
    p = claim.get("path")
    if p is None:
        return []
    return list(p) if isinstance(p, list) else [p]


def _coverage_root_of(path: str, roots: list[str]) -> str | None:
    norm = path.rstrip("/")
    for root in roots:
        rn = root.rstrip("/")
        # A bare "." root (flat repository) covers every path.
        if rn in ("", "."):
            return root
        if norm == rn or norm.startswith(rn + "/"):
            return root
    return None


def _first_sentence(text: str) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    return re.split(r"(?<=[.!?])\s", text, maxsplit=1)[0]


def _strip_terminal_punct(text: str) -> str:
    return text.rstrip(" .!?")


def build_file_buckets(claims: list[dict], roots: list[str]) -> dict[str, list[dict]]:
    """Map each covered file path to the status claims that name it."""
    buckets: dict[str, list[dict]] = {}
    for claim in claims:
        if claim.get("kind") != "status":
            continue
        for path in _path_list(claim):
            if _coverage_root_of(path, roots) is None:
                continue
            buckets.setdefault(path, []).append(claim)
    return buckets


def select_files(buckets: dict[str, list[dict]], roots: list[str], n: int, rng: random.Random):
    """Spread up to n files across coverage roots, preferring >=2-claim files."""
    by_root: dict[str, list[tuple[str, list[dict]]]] = {}
    for path, claims in buckets.items():
        root = _coverage_root_of(path, roots)
        by_root.setdefault(root, []).append((path, claims))
    for root in by_root:
        by_root[root].sort(key=lambda pc: (-len(pc[1]), pc[0]))

    roots_order = sorted(by_root)
    rng.shuffle(roots_order)

    chosen: list[tuple[str, list[dict]]] = []
    active = [r for r in roots_order if by_root[r]]
    i = 0
    while len(chosen) < n and active:
        root = active[i % len(active)]
        chosen.append(by_root[root].pop(0))
        if not by_root[root]:
            active.remove(root)
        else:
            i += 1
    return chosen


def build_target(path: str, claims: list[dict], rng: random.Random) -> dict:
    ordered = sorted(claims, key=lambda c: c["id"])
    target_idx = rng.randrange(len(ordered))
    target = ordered[target_idx]
    siblings = [c for j, c in enumerate(ordered) if j != target_idx]

    # x is a noun phrase, not a question: the fleet-probe prompt renders it
    # as "I want to change ${x}." -- a leading question or a trailing
    # period here would double up with that template. The note is
    # appended as a parenthetical gloss rather than a "which ..." relative
    # clause, since a note can be a noun phrase or a predicate and only
    # the parenthetical form reads correctly for both.
    component = target.get("component") or path
    note_lead = _strip_terminal_punct(_first_sentence(target.get("note", "")))
    x = f"{component} ({note_lead})" if note_lead else component

    expect = target["id"]
    if siblings:
        why = "; ".join(
            f"{s['id']}: "
            f"{_strip_terminal_punct(_first_sentence(s.get('note', ''))) or s.get('component') or 'a different concern'}"
            ", a different concern on the same path"
            for s in siblings
        )
        expect += f" ({why})"

    return {"x": x, "expect": expect}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run", required=True, help="path to run.json")
    parser.add_argument("--seed", type=int, default=0, help="RNG seed for reproducible target selection")
    parser.add_argument("--n", type=int, default=5, help="number of fleet-probe targets (default 5)")
    parser.add_argument("--status", help="path to STATUS.yaml (defaults to run.repo/STATUS.yaml)")
    parser.add_argument("--models", help="path to a JSON file with role model overrides")
    args = parser.parse_args(argv)

    run = load_run(args.run)
    status_path = Path(args.status) if args.status else run.repo / "STATUS.yaml"
    data = yaml.safe_load(status_path.read_text(encoding="utf-8")) or {}
    claims = data.get("claims") or []
    roots = ((data.get("meta") or {}).get("coverage") or {}).get("roots") or []

    if not roots:
        print(f"{status_path}: meta.coverage.roots is empty, nothing to spread targets across", file=sys.stderr)
        return 1

    rng = random.Random(args.seed)
    buckets = build_file_buckets(claims, roots)
    chosen = select_files(buckets, roots, args.n, rng)
    targets = [build_target(path, group, rng) for path, group in chosen]

    payload = {"repo": str(run.repo), "python": str(run.python), "targets": targets}
    if args.models:
        payload["models"] = json.loads(Path(args.models).read_text(encoding="utf-8"))

    out_path = args_path(run, "i10")
    out_path.write_text(json.dumps(payload, indent=1, ensure_ascii=False), encoding="utf-8")

    print(f"picked {len(targets)}/{args.n} targets from {len(buckets)} covered files across {len(roots)} coverage roots")
    print(f"wrote {out_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
