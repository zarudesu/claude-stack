#!/usr/bin/env python3
"""Queue of deferred contract drift: .ground-truth/pending.yaml.

Usage: python3 tools/ground_truth/gt_pending.py --root DIR record --session ID < verify-output
       python3 tools/ground_truth/gt_pending.py --root DIR gate
       python3 tools/ground_truth/gt_pending.py --root DIR summary
       python3 tools/ground_truth/gt_pending.py --root DIR batch --verdicts FILE
       python3 tools/ground_truth/gt_pending.py --root DIR report [--format md]

Stop blocks only on serious findings: every FAIL of verify --mode=sync and of
the session guard (red touched check, raise without proof, debt closed without
proof, new claim check without assertions, missing mutation description,
broken environment). Maintenance drift of the classes below is queued instead:
one entry per object and class, removed as soon as the check that produced
it no longer reports it. The sessions that saw an entry are local state in the
git dir (ground-truth-pending-sessions.json), so a repeated Stop without new
findings leaves the committed file as it was. Other warnings (budget,
skipped tool, audit due, diff-relative advice, line shifts from
remap_line_refs) are printed as before and never queued.

A queue blocks Stop only when an area the working tree touches has more than
limits.per_area entries or an escalated entry, and only under blocking
enforcement; an unreadable queue exits 3 there. verify.py --mode=full fails on
more than limits.total entries or on an entry escalated by age or batch
reviews, counting only entries the current run still finds.

batch applies the verdicts of a batch review and appends one block to
.ground-truth/pending-log.yaml; report prints the queue, the last run's metrics,
alarms and class recommendations (references/batch-review.md).
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import date
import fnmatch
import json
import os
from pathlib import Path
import re
import statistics
import subprocess
import sys

import yaml

QUEUE = ".ground-truth/pending.yaml"
MODEL = ".ground-truth/model.yaml"
LOG = ".ground-truth/pending-log.yaml"
SESSIONS = "ground-truth-pending-sessions.json"
STORED = ("key", "area", "class", "first_seen", "evidence", "batches_survived", "kept")
VERDICTS = ("Keep", "Update", "Consolidate", "Replace", "Delete")
PLACEHOLDERS = {"", "-", "?", "...", "todo", "tbd", "n/a", "na", "none"}
# Defaults; a `limits:` mapping at the top of the queue file overrides them.
LIMITS = {"total": 30, "per_area": 5, "max_age_days": 7, "max_batches": 2, "max_sessions": 3}
# Deferred classes from verify --mode=sync WARN lines; the group `obj` names the
# object, its part before ":" is the path the area comes from.
STATUS_CLASSES = {
    "missing-path": re.compile(r"^path (['\"])(?P<obj>.+?)\1 does not exist"),
    "uncovered": re.compile(r"^(?P<obj>.+?) is not covered by any claim"),
    "cited-line": re.compile(r"^cited line (?P<obj>.+?) does not contain «"),
    "no-assertions": re.compile(r"^(?P<obj>.+): test has no assertions$"),
    "open-debt": re.compile(r"blocking debt on an implemented claim"),
    "no-canary": re.compile(r"^coverage root (?P<obj>.+?): \d+ implemented/partial claims, no canary"),
}
# Deferred classes from gt_context.py: area whose review receipt is stale,
# watched file no area owns.
MEMORY_CLASSES = {"stale-area", "unclassified"}
_WARN_LINE = re.compile(r"^\[WARN\] (\S+) : (.*)$")


class Unreadable(ValueError):
    """The queue file does not parse (a merge conflict, a bad hand edit)."""


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


def escalation(entry, limits, today, sessions=0):
    since = "kept" if entry.get("kept") else "first seen"
    age = (today - date.fromisoformat(str((entry.get("kept") or entry["first_seen"])["date"]))).days
    if age > limits["max_age_days"]:
        return f"{since} {age}d ago (> {limits['max_age_days']}d)"
    if entry.get("batches_survived", 0) >= limits["max_batches"]:
        return f"survived {entry['batches_survived']} batch reviews (>= {limits['max_batches']})"
    if sessions >= limits["max_sessions"]:
        return f"seen in {sessions} sessions (>= {limits['max_sessions']})"
    return None


def _sessions_path(root):
    """Local, never committed: the git dir, or .ground-truth outside a repository."""
    git_dir = _git(root, "rev-parse", "--git-dir").strip()
    return (Path(root) / git_dir if git_dir else Path(root) / ".ground-truth") / SESSIONS


def _sessions(root):
    try:
        data = json.loads(_sessions_path(root).read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(path, text):
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def _dump(data, limits, entries):
    head = {"limits": limits, **({"classes": data["classes"]} if data.get("classes") else {})}
    pending = [{f: entries[k][f] for f in STORED if f in entries[k]} for k in sorted(entries)]
    return yaml.safe_dump({**head, "pending": pending}, sort_keys=False, allow_unicode=True)


def _mark(entries, limits, sessions):
    today = date.today()
    for key, entry in entries.items():
        reason = escalation(entry, limits, today, len(sessions.get(key, [])))
        entry["escalated"] = reason is not None
        entry.pop("reason", None)
        if reason:
            entry["reason"] = reason


def load(root, local=False):
    """(file data, effective limits, entries by key, raw text); escalation is derived on every read,
    by session count only with `local` (the Stop gate and the session summary, never CI)."""
    path = Path(root) / QUEUE
    try:
        text = path.read_text() if path.is_file() else ""
        data = (yaml.safe_load(text) or {}) if text else {}
        if not isinstance(data, dict) or not isinstance(data.get("pending", []), list):
            raise ValueError(f"{QUEUE} must be a mapping with a pending list")
        if any(v not in ("ignore", "block") for v in (data.get("classes") or {}).values()):
            raise ValueError(f"{QUEUE} classes: values must be ignore or block")
        limits = {**LIMITS, **(data.get("limits") or {})}
        entries = {entry["key"]: entry for entry in data.get("pending") or []}
        _mark(entries, limits, _sessions(root) if local else {})
    except (OSError, ValueError, KeyError, TypeError, AttributeError, yaml.YAMLError) as exc:
        raise Unreadable(" ".join(str(exc).split())) from exc
    return data, limits, entries, text


def record(root, findings, classes, session, unjudged=lambda key: False):
    """Merge (class, object, area, evidence) findings; drop entries of `classes` not found,
    except those `unjudged` says this run could not check."""
    data, limits, entries, before = load(root)
    ignored = {c for c, v in (data.get("classes") or {}).items() if v == "ignore"}
    first = {"date": date.today().isoformat(), "rev": _git(root, "rev-parse", "--short", "HEAD").strip() or "-"}
    added, seen = [], set()
    for cls, obj, area, evidence in findings:
        if cls in ignored:
            continue
        key = f"{cls}:{obj}"
        seen.add(key)
        entry = entries.get(key)
        if entry is None:
            entries[key] = {"key": key, "area": area, "class": cls, "first_seen": dict(first),
                            "evidence": evidence[:160], "batches_survived": 0}
            added.append(key)
        else:
            entry.update(area=area, evidence=evidence[:160])
    resolved = sorted(k for k, e in entries.items() if e["class"] in classes and k not in seen and not unjudged(k))
    for key in resolved:
        del entries[key]
    after = _dump(data, limits, entries)
    if after != before and (before or entries):
        path = Path(root) / QUEUE
        path.parent.mkdir(exist_ok=True)
        _write(path, after)
    old = _sessions(root)
    sessions = {k: list(v) for k, v in old.items() if k in entries}
    for key in seen:
        if session not in sessions.setdefault(key, []):
            sessions[key].append(session)
    if sessions != old:
        _write(_sessions_path(root), json.dumps(sessions, sort_keys=True))
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


def _blocking(root):
    blocking = False
    for name, get in (("STATUS.yaml", lambda d: (d.get("meta") or {}).get("enforcement")),
                      (MODEL, lambda d: d.get("enforcement"))):
        path = Path(root) / name
        data = yaml.safe_load(path.read_text()) if path.is_file() else None
        blocking |= isinstance(data, dict) and get(data) == "blocking"
    return blocking


def gate(root, touched=None):
    """One line and 1 when blocking enforcement meets an overflowing/escalated touched area;
    3 under blocking enforcement when the queue can not be read. `touched` defaults to the
    areas of the working tree's changes; "global" never counts as touched."""
    blocking = _blocking(root)
    label = "ground-truth" if blocking else "ground-truth (advisory)"
    try:
        data, limits, entries, _ = load(root, local=True)
    except Unreadable as exc:
        print(f"{label}: pending.yaml unreadable: {exc}; resolve conflict or delete the file", file=sys.stderr)
        return 3 if blocking else 0
    if not entries:
        return 0
    if touched is None:
        areas = model_areas(root)
        changed = set(_git(root, "diff", "--name-only", "HEAD").split("\n"))
        changed |= set(_git(root, "ls-files", "--others", "--exclude-standard").split("\n"))
        touched = {area_of(p, areas) for p in changed if p}
    block = {c for c, v in (data.get("classes") or {}).items() if v == "block"}
    reasons = []
    for area in sorted(set(touched) - {"global"}):
        mine = [e for e in entries.values() if e["area"] == area]
        if len(mine) > limits["per_area"]:
            reasons.append(f"area {area} has {len(mine)}/{limits['per_area']} entries")
        reasons += [f"{e['key']} escalated: {e['reason']}" if e["escalated"] else f"{e['key']}: class {e['class']} blocks"
                    for e in mine if e["escalated"] or e["class"] in block]
    if not reasons:
        return 0
    print(f"{label}: pending queue blocks this area: {'; '.join(reasons)} -- fix these entries "
          f"(see {QUEUE}) and stop again", file=sys.stderr)
    return 1 if blocking else 0


def _log(root):
    path = Path(root) / LOG
    return (yaml.safe_load(path.read_text()) or []) if path.is_file() else []


def _age(entry, today):
    return (today - date.fromisoformat(str(entry["first_seen"]["date"]))).days


def summary(root):
    _, limits, entries, _ = load(root, local=True)
    if entries:
        today = date.today()
        oldest = max(_age(e, today) for e in entries.values())
        escalated = sum(e["escalated"] for e in entries.values())
        log = _log(root)
        idle = (today - date.fromisoformat(str(log[-1]["date"]))).days if log else oldest
        due = [reason for reason, hit in ((f"queue at {len(entries)}/{limits['total']}", len(entries) >= 0.7 * limits["total"]),
                                          (f"no batch review for {idle}d", idle > 3.5),
                                          (f"{escalated} escalated", escalated)) if hit]
        print(f"ground-truth pending {len(entries)}/{limits['total']}, oldest {oldest}d, escalated {escalated}"
              + (f"; batch review due: {', '.join(due)}" if due else ""))


def _verdict_problem(item, entries, given, claims):
    key, verdict, evidence = item.get("key"), item.get("verdict"), item.get("evidence")
    if key not in entries:
        return "not in the queue"
    if key in given:
        return "given twice"
    if verdict not in VERDICTS:
        return f"verdict must be one of {', '.join(VERDICTS)}"
    if not isinstance(evidence, str) or evidence.strip().lower() in PLACEHOLDERS:
        return "evidence is empty or a placeholder"
    if verdict == "Consolidate" and (item.get("into") not in entries or item["into"] == key):
        return "Consolidate needs into: <another queued key>"
    if verdict == "Replace" and not set(re.findall(r"STATUS\.yaml#([\w.-]+)", evidence)) & claims:
        return "Replace evidence must name STATUS.yaml#<id> of an existing claim"
    if item.get("noise") and verdict != "Delete":
        return "noise is only for Delete"
    return None


def batch(root, verdicts_file):
    """Apply batch-review verdicts all or nothing and append one block to the log; 1 on a bad verdict."""
    data, limits, entries, _ = load(root)
    items = yaml.safe_load(Path(verdicts_file).read_text()) or []
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        raise ValueError(f"{verdicts_file} must be a list of mappings")
    status = Path(root) / "STATUS.yaml"
    claims = {c.get("id") for c in (yaml.safe_load(status.read_text()) or {}).get("claims") or []
              if isinstance(c, dict)} if status.is_file() else set()
    errors, given = [], {}
    for item in items:
        problem = _verdict_problem(item, entries, given, claims)
        if problem:
            errors.append(f"ground-truth pending batch: {item.get('key')}: {problem}")
        given.setdefault(item.get("key"), item)
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    log = _log(root)
    today, before = date.today(), set(entries)
    previous = set(log[-1]["keys"]) if log else set()
    verdicts, closed, noise, carried = Counter(), Counter(), Counter(), 0
    escalated = Counter(e["class"] for e in entries.values() if e["escalated"])
    for key in sorted(before):
        entry, item = entries[key], given.get(key)
        if item is None:
            entry["batches_survived"] = entry.get("batches_survived", 0) + 1
            carried += 1
            continue
        verdicts[item["verdict"]] += 1
        if item["verdict"] == "Keep":
            entry.update(kept={"date": today.isoformat(), "evidence": item["evidence"][:160]}, batches_survived=0)
            continue
        del entries[key]
        closed[entry["class"]] += 1
        noise[entry["class"]] += bool(item.get("noise"))
    ages = [_age(e, today) for e in entries.values()] or [0]
    block = {"date": today.isoformat(), "rev": _git(root, "rev-parse", "--short", "HEAD").strip() or "-",
             "keys": sorted(entries),
             "flow": {"in": len(before - previous), "out": len((previous | before) - set(entries))},
             "verdicts": {v: verdicts[v] for v in VERDICTS}, "closed": dict(closed),
             "noise": {c: n for c, n in noise.items() if n}, "escalated": dict(escalated), "carried": carried,
             "age": {"median": statistics.median_low(ages), "max": max(ages)},
             "auto_repaired": 0}  # B3 fills it
    _write(Path(root) / QUEUE, _dump(data, limits, entries))
    old = (Path(root) / LOG).read_text() if log else ""
    _write(Path(root) / LOG, old + yaml.safe_dump([block], sort_keys=False, allow_unicode=True))
    print(f"ground-truth pending batch: {len(items)} verdicts, {carried} carried, {len(entries)} left")
    return 0


def report(root, fmt="text"):
    """Queue, metrics of the last batch review, alarms and class recommendations; md fits an issue body."""
    _, limits, entries, _ = load(root)
    log = _log(root)
    areas = Counter(e["area"] for e in entries.values())
    queue = [f"{len(entries)}/{limits['total']} entries; by area: "
             + (", ".join(f"{a} {n}" for a, n in sorted(areas.items())) or "-")]
    queue += [f"{e['key']} escalated: {e['reason']}" for e in entries.values() if e["escalated"]]
    run, alarms = [], []
    if log:
        last = log[-1]
        decided = sum(last["verdicts"].values())
        run = [f"{last['date']} at {last['rev']}: inflow {last['flow']['in']}, outflow {last['flow']['out']}",
               f"age median {last['age']['median']}d, max {last['age']['max']}d",
               f"auto-repaired {last['auto_repaired']}/{last['flow']['out']} of outflow, carried without verdict "
               f"{last['carried']}/{last['carried'] + decided}, Delete as noise {sum(last['noise'].values())}/{decided}",
               "verdicts: " + ", ".join(f"{v} {n}" for v, n in last["verdicts"].items())]
    if len(log) >= 2 and all(b["flow"]["out"] < b["flow"]["in"] for b in log[-2:]):
        alarms.append("outflow below inflow in the last 2 batch reviews: the queue grows, owner decision needed")
    recent = log[-4:]
    noise, closed, escalated = Counter(), Counter(), Counter()
    for block in recent:
        noise.update(block["noise"])
        closed.update(block["closed"])
        escalated.update(block["escalated"])
    advice = [f"demote {c}: {noise[c]}/{closed[c]} closed as noise in the last {len(recent)} reviews "
              f"(classes: {{{c}: ignore}})" for c in sorted(closed) if noise[c] * 2 > closed[c]]
    if escalated:
        cls, n = max(sorted(escalated.items()), key=lambda item: item[1])
        advice.append(f"promote {cls}: {n} escalations in the last {len(recent)} reviews (classes: {{{cls}: block}})")
    out = []
    for title, lines in (("Queue", queue), ("Last batch review", run or ["none yet"]),
                         ("Alarms", alarms or ["none"]), ("Class recommendations", advice or ["none"])):
        out.append(f"## {title}\n\n" + "".join(f"- {line}\n" for line in lines) if fmt == "md"
                   else f"{title}:\n" + "".join(f"  {line}\n" for line in lines))
    print(("\n" if fmt == "md" else "").join(out), end="")


def ci_problems(root, live, classes):
    """FAIL lines for CI: total limit exceeded or an entry escalated. Entries of `classes`
    count only when `live`, the keys this run found, still has them."""
    _, limits, entries, _ = load(root)
    entries = {k: e for k, e in entries.items() if e["class"] not in classes or k in live}
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
    commands.add_parser("batch").add_argument("--verdicts", type=Path, required=True)
    commands.add_parser("report").add_argument("--format", choices=("text", "md"), default="text")
    args = parser.parse_args(argv)
    try:
        if args.command == "record":
            try:
                record(args.root, status_findings(args.root, sys.stdin), set(STATUS_CLASSES), args.session)
            except Unreadable:
                return 3  # the gate that follows reports it once
            return 0
        if args.command == "gate":
            return gate(args.root)
        if args.command == "batch":
            return batch(args.root, args.verdicts)
        if args.command == "report":
            report(args.root, args.format)
            return 0
        summary(args.root)
        return 0
    except (OSError, ValueError, KeyError, TypeError, AttributeError, yaml.YAMLError) as exc:
        print(f"ground-truth pending: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
