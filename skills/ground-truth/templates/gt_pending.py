#!/usr/bin/env python3
"""Queue of deferred contract drift: .ground-truth/pending.yaml.

Usage: python3 tools/ground_truth/gt_pending.py --root DIR record --session ID < verify-output
       python3 tools/ground_truth/gt_pending.py --root DIR gate
       python3 tools/ground_truth/gt_pending.py --root DIR summary

Stop blocks only on serious findings: every FAIL of verify --mode=sync and of
the session guard (red touched check, raise without proof, debt closed without
proof, new claim check without assertions, missing mutation description,
broken environment). Maintenance drift of the classes below is queued instead:
one entry per object and class, counted once per session, removed as soon as
the check that produced it no longer reports it. Other warnings (budget,
skipped tool, audit due, diff-relative advice, line shifts from
remap_line_refs) are printed as before and never queued.

A queue blocks Stop only when an area the working tree touches has more than
limits.per_area entries or an escalated entry, and only under blocking
enforcement. verify.py --mode=full fails on more than limits.total entries or
on any escalated entry.
"""
from __future__ import annotations

import argparse
from datetime import date
import fnmatch
from pathlib import Path
import re
import subprocess
import sys

import yaml

QUEUE = ".ground-truth/pending.yaml"
MODEL = ".ground-truth/model.yaml"
# Defaults; a `limits:` mapping at the top of the queue file overrides them.
LIMITS = {"total": 30, "per_area": 5, "max_age_days": 7, "max_batches": 2, "max_sessions": 3}
# Deferred classes from verify --mode=sync WARN lines; the group `obj` names the
# object, its part before ":" is the path the area comes from.
STATUS_CLASSES = {
    "missing-path": re.compile(r"^path '(?P<obj>[^']+)' does not exist"),
    "uncovered": re.compile(r"^(?P<obj>\S+) is not covered by any claim"),
    "cited-line": re.compile(r"^cited line (?P<obj>\S+) does not contain"),
    "no-assertions": re.compile(r"^(?P<obj>\S+): test has no assertions$"),
    "open-debt": re.compile(r"blocking debt on an implemented claim"),
    "no-canary": re.compile(r"^coverage root (?P<obj>\S+): .* no canary"),
}
# Deferred classes from gt_context.py: area whose review receipt is stale,
# watched file no area owns.
MEMORY_CLASSES = {"stale-area", "unclassified"}
_WARN_LINE = re.compile(r"^\[WARN\] (\S+) : (.*)$")


def _git(root, *args):
    proc = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
    return proc.stdout if proc.returncode == 0 else ""


def _matches(path, pattern):
    pattern = pattern.removeprefix("./").rstrip("/") or "."
    return (pattern in (".", "*", "**") or path == pattern or path.startswith(pattern + "/")
            or fnmatch.fnmatchcase(path, pattern))


def model_areas(root):
    path = Path(root) / MODEL
    model = yaml.safe_load(path.read_text()) if path.is_file() else None
    return model.get("areas") or [] if isinstance(model, dict) else []


def area_of(path, areas):
    """First model area whose sources/docs own the path, else its top directory, else global."""
    for area in areas:
        if any(_matches(path, p) for p in area.get("sources", []) + area.get("docs", [])):
            return area["id"]
    top, sep, _ = path.partition("/")
    return top if sep else "global"


def escalation(entry, limits, today):
    age = (today - date.fromisoformat(str(entry["first_seen"]["date"]))).days
    if age > limits["max_age_days"]:
        return f"first seen {age}d ago (> {limits['max_age_days']}d)"
    if entry.get("batches_survived", 0) >= limits["max_batches"]:
        return f"survived {entry['batches_survived']} batch reviews (>= {limits['max_batches']})"
    if entry.get("sessions", 0) >= limits["max_sessions"]:
        return f"seen in {entry['sessions']} sessions (>= {limits['max_sessions']})"
    return None


def _mark(entries, limits):
    today = date.today()
    for entry in entries.values():
        reason = escalation(entry, limits, today)
        entry["escalated"] = reason is not None
        entry.pop("reason", None)
        if reason:
            entry["reason"] = reason


def load(root):
    """(file data, effective limits, entries by key, raw text); escalation is derived on every read."""
    path = Path(root) / QUEUE
    text = path.read_text() if path.is_file() else ""
    data = (yaml.safe_load(text) or {}) if text else {}
    if not isinstance(data, dict) or not isinstance(data.get("pending", []), list):
        raise ValueError(f"{QUEUE} must be a mapping with a pending list")
    limits = {**LIMITS, **(data.get("limits") or {})}
    entries = {entry["key"]: entry for entry in data.get("pending") or []}
    _mark(entries, limits)
    return data, limits, entries, text


def record(root, findings, classes, session):
    """Merge (class, object, area, evidence) findings; drop entries of `classes` not found."""
    data, limits, entries, before = load(root)
    stamp = {"date": date.today().isoformat(), "rev": _git(root, "rev-parse", "--short", "HEAD").strip() or "-",
             "session": session}
    added, seen = [], set()
    for cls, obj, area, evidence in findings:
        key = f"{cls}:{obj}"
        seen.add(key)
        entry = entries.get(key)
        if entry is None:
            entries[key] = {"key": key, "area": area, "class": cls, "first_seen": {"date": stamp["date"], "rev": stamp["rev"]},
                            "last_seen": dict(stamp), "sessions": 1, "evidence": evidence[:160],
                            "batches_survived": 0, "escalated": False}
            added.append(key)
            continue
        if entry["last_seen"].get("session") != session:
            entry["sessions"] += 1
        entry.update(area=area, last_seen=dict(stamp), evidence=evidence[:160])
    resolved = sorted(k for k, e in entries.items() if e["class"] in classes and k not in seen)
    for key in resolved:
        del entries[key]
    _mark(entries, limits)
    data = {**({"limits": data["limits"]} if data.get("limits") else {}),
            "pending": [entries[k] for k in sorted(entries)]}
    after = yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
    if after != before and (before or entries):
        path = Path(root) / QUEUE
        path.parent.mkdir(exist_ok=True)
        path.write_text(after)
    for verb, keys in (("queued", sorted(added)), ("resolved", resolved)):
        if keys:
            more = f" (+{len(keys) - 5} more)" if len(keys) > 5 else ""
            print(f"ground-truth pending: {verb} {len(keys)}: {', '.join(keys[:5])}{more}", file=sys.stderr)


def status_findings(root, lines):
    """Deferred-class findings from verify.py --mode=sync --show-warn output."""
    status = yaml.safe_load((Path(root) / "STATUS.yaml").read_text()) or {}
    claim_path = {}
    for claim in status.get("claims") or []:
        path = claim.get("path") if isinstance(claim, dict) else None
        path = path[0] if isinstance(path, list) and path else path
        if isinstance(path, str):
            claim_path[claim.get("id")] = path
    for debt in status.get("debt") or []:
        if isinstance(debt, dict) and debt.get("ref") in claim_path:
            claim_path[debt.get("id")] = claim_path[debt["ref"]]
    areas = model_areas(root)
    findings = []
    for line in lines:
        warn = _WARN_LINE.match(line.rstrip("\n"))
        if not warn:
            continue
        cid, message = warn.groups()
        for cls, pattern in STATUS_CLASSES.items():
            m = pattern.search(message)
            if not m:
                continue
            item = m.groupdict().get("obj") or ""
            path = item.split(":")[0] if item else claim_path.get(cid)
            obj = ":".join(x for x in (cid if cid != "-" else "", item) if x)
            findings.append((cls, obj, area_of(path, areas) if path else "global", message))
            break
    return findings


def gate(root):
    """One line and 1 when blocking enforcement meets an overflowing/escalated touched area."""
    _, limits, entries, _ = load(root)
    if not entries:
        return 0
    areas = model_areas(root)
    changed = set(_git(root, "diff", "--name-only", "HEAD").split("\n"))
    changed |= set(_git(root, "ls-files", "--others", "--exclude-standard").split("\n"))
    touched = {area_of(p, areas) for p in changed if p}
    reasons = []
    for area in sorted(touched):
        mine = [e for e in entries.values() if e["area"] == area]
        if len(mine) > limits["per_area"]:
            reasons.append(f"area {area} has {len(mine)}/{limits['per_area']} entries")
        reasons += [f"{e['key']} escalated: {e['reason']}" for e in mine if e["escalated"]]
    if not reasons:
        return 0
    blocking = False
    for name, get in (("STATUS.yaml", lambda d: (d.get("meta") or {}).get("enforcement")),
                      (MODEL, lambda d: d.get("enforcement"))):
        path = Path(root) / name
        data = yaml.safe_load(path.read_text()) if path.is_file() else None
        blocking |= isinstance(data, dict) and get(data) == "blocking"
    label = "ground-truth" if blocking else "ground-truth (advisory)"
    print(f"{label}: pending queue blocks this area: {'; '.join(reasons)} -- fix these entries "
          f"(see {QUEUE}) and stop again", file=sys.stderr)
    return 1 if blocking else 0


def summary(root):
    _, limits, entries, _ = load(root)
    if entries:
        today = date.today()
        oldest = max((today - date.fromisoformat(str(e["first_seen"]["date"]))).days for e in entries.values())
        escalated = sum(e["escalated"] for e in entries.values())
        print(f"ground-truth pending {len(entries)}/{limits['total']}, oldest {oldest}d, escalated {escalated}")


def ci_problems(root):
    """FAIL lines for verify.py --mode=full: total limit exceeded or an entry escalated."""
    _, limits, entries, _ = load(root)
    problems = []
    if len(entries) > limits["total"]:
        problems.append(f"pending queue has {len(entries)}/{limits['total']} entries; run the batch review of {QUEUE}")
    problems += [f"pending {e['key']} escalated: {e['reason']}" for e in entries.values() if e["escalated"]]
    return problems


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("record").add_argument("--session", required=True)
    commands.add_parser("gate")
    commands.add_parser("summary")
    args = parser.parse_args(argv)
    try:
        if args.command == "record":
            record(args.root, status_findings(args.root, sys.stdin), set(STATUS_CLASSES), args.session)
            return 0
        if args.command == "gate":
            return gate(args.root)
        summary(args.root)
        return 0
    except (OSError, ValueError, KeyError, TypeError, AttributeError, yaml.YAMLError) as exc:
        print(f"ground-truth pending: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
