#!/usr/bin/env python3
"""Check a generated STATUS.yaml against a selftest expected.json.

For every expected component: a claim whose path contains that
substring exists with the expected status (and check_kind, if given).
For every debt_must_exist entry: an open debt whose description
carries at least one keyword from "about" and whose description or
ref'd claim path mentions "path". At least canary_min claims carry
canary: true. meta.coverage.roots equals coverage_roots, as sets.

Prints one row per expectation and exits 1 if any of them missed.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import yaml


def _path_list(claim: dict) -> list[str]:
    p = claim.get("path")
    if p is None:
        return []
    return list(p) if isinstance(p, list) else [p]


def _ref_path(debt: dict, claims: list[dict]) -> str:
    ref = debt.get("ref")
    for c in claims:
        if c.get("id") == ref:
            return " ".join(_path_list(c))
    return ""


def check_component(claims: list[dict], component: str, spec: dict) -> tuple[bool, str]:
    matches = [c for c in claims if any(component in p for p in _path_list(c))]
    token = spec.get("or_id_contains")
    if not matches and token:
        # an absent/design-only claim is anchored at an existing file (the
        # doc or registry that promises the missing one), so the concern
        # is found by id, not by the missing path
        matches = [c for c in claims if token in (c.get("id") or "")]
    if not matches:
        return False, f"no claim covers a path containing {component!r}" + (f" or an id containing {token!r}" if token else "")

    want_status = spec.get("status")
    status_hits = [c for c in matches if c.get("status") == want_status]
    found = sorted({c.get("status") for c in matches})
    split = spec.get("or_split")
    if not status_hits and split and set(split) <= set(found):
        return True, f"{len(matches)} claim(s), split into {found} instead of status={want_status!r} ok"
    if not status_hits:
        return False, f"{len(matches)} claim(s) found, none with status={want_status!r} (found: {found})"

    want_kind = spec.get("check_kind")
    if want_kind:
        kind_hits = [c for c in status_hits if c.get("check_kind") == want_kind]
        if not kind_hits:
            found = sorted({c.get("check_kind") for c in status_hits})
            return False, f"status matched but no check_kind={want_kind!r} (found: {found})"

    return True, f"{len(matches)} claim(s), status={want_status!r} ok"


def check_debt(claims: list[dict], debts: list[dict], item: dict) -> tuple[bool, str]:
    about = item.get("about", "")
    path = item.get("path", "")
    path_lower = path.lower()
    keywords = [w for w in re.findall(r"[A-Za-z0-9_]+", about.lower()) if len(w) >= 4]

    for d in debts:
        if d.get("state") != "open":
            continue
        desc = (d.get("description") or "").lower()
        if keywords and not any(k in desc for k in keywords):
            continue
        ref_path = _ref_path(d, claims).lower()
        if path_lower and path_lower not in desc and path_lower not in ref_path:
            continue
        return True, f"matched debt {d.get('id')!r}"

    return False, f"no open debt matching keywords {keywords} and path {path!r}"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--status", required=True, help="path to a generated STATUS.yaml")
    parser.add_argument("--expected", required=True, help="path to the selftest expected.json")
    parser.add_argument("--repo", help="repo root, only used to resolve relative --status/--expected paths")
    args = parser.parse_args(argv)

    base = Path(args.repo) if args.repo else Path(".")
    status_path = base / args.status if not Path(args.status).is_absolute() else Path(args.status)
    expected_path = base / args.expected if not Path(args.expected).is_absolute() else Path(args.expected)

    data = yaml.safe_load(status_path.read_text(encoding="utf-8")) or {}
    expected = json.loads(expected_path.read_text(encoding="utf-8"))

    claims = data.get("claims") or []
    debts = data.get("debt") or []

    rows: list[tuple[str, str, bool]] = []

    for component, spec in (expected.get("components") or {}).items():
        ok, note = check_component(claims, component, spec)
        rows.append((f"component {component}", note, ok))

    for item in expected.get("debt_must_exist") or []:
        ok, note = check_debt(claims, debts, item)
        rows.append((f"debt about {item.get('about')!r} @ {item.get('path')}", note, ok))

    canary_min = expected.get("canary_min", 0)
    canary_count = sum(1 for c in claims if c.get("canary"))
    rows.append((f"canary_min {canary_min}", f"found {canary_count}", canary_count >= canary_min))

    if "coverage_roots" in expected:
        want_roots = expected["coverage_roots"]
        got_roots = ((data.get("meta") or {}).get("coverage") or {}).get("roots") or []
        want_norm = {r.rstrip("/") for r in want_roots}
        got_norm = {r.rstrip("/") for r in got_roots}
        rows.append((f"coverage_roots {want_roots}", f"found {got_roots}", got_norm == want_norm))

    width = max((len(r[0]) for r in rows), default=10)
    for expectation, found, ok in rows:
        print(f"{expectation:<{width}}  {found}  [{'OK' if ok else 'MISS'}]")

    passed = sum(1 for r in rows if r[2])
    print(f"\n{passed}/{len(rows)} expectations met")

    return 0 if passed == len(rows) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
