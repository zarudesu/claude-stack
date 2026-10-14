#!/usr/bin/env python3
"""Move a legacy STATUS.yaml into the sharded .ground-truth/ layout.

Usage: python3 tools/ground_truth/gt.py migrate [--write] [--by path|component]
           [--depth 3] [--max-claims 25] [--min-claims 3] [--drop-resolved]
           [--skill DIR] [--root DIR]

Without --write prints the plan and changes nothing. --write writes
.ground-truth/contract.yaml, debt.yaml and areas/<id>.yaml, deletes
STATUS.yaml and checks the result: the new layout loads to the same data
(E1) and verify --mode=full, with no check executed, reports the same
FAIL/WARN findings (E2). Any difference puts every file back; otherwise
the contract change is staged for one commit. Claims, debt entries, meta
and edges keep their original text; a block that does not read back to the
same value is written with yaml.safe_dump instead. A model.yaml source
STATUS.yaml is dropped and the receipts of those areas, if fresh before,
are refreshed.

Exit 0: migrated, or already sharded. 1: not equivalent, rolled back.
2: bad arguments. 3: contract error, mixed or missing layout, failed precheck.
"""
from __future__ import annotations

import argparse
import collections
import datetime
import os
import re
import subprocess
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

import contract_lib  # noqa: E402
import gt_doctor  # noqa: E402
import gt_store  # noqa: E402
from gt_pending import _matches  # noqa: E402
from gt_store import STATUS, ContractError  # noqa: E402

GLOBAL = "global"
OVERSIZED = 40
WORKSPACE = ".ground-truth/workspace"
LEGACY_KEYS = {"schema_version", "meta", "edges", "claims", "debt"}
# Contract file names and file:line inside them differ between layouts by design.
_CONTRACT_REF = re.compile(
    r"(STATUS\.yaml|\.ground-truth/(?:contract|debt)\.yaml|\.ground-truth/areas/[\w.-]+\.yaml"
    r"|contract \(\.ground-truth/\))(:\d+)?"
)


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def _span(lines: list[str], start: int, end_mark) -> tuple[int, int]:
    """Lines start..end of a node, without the blank lines and the outer
    comments that only precede whatever comes next."""
    end = end_mark.line
    if end < len(lines) and lines[end][:end_mark.column].strip():
        end += 1
    while end > start + 1 and (not lines[end - 1].strip() or (
            lines[end - 1].lstrip().startswith("#") and _indent(lines[end - 1]) <= _indent(lines[start]))):
        end -= 1
    return start, end


def legacy_blocks(text: str) -> tuple[dict, list[str]]:
    """meta/edges -> their lines, (claims|debt, index) -> the lines of one
    entry, and the comment lines that belong to none of them."""
    lines = text.splitlines()
    spans = {}
    for key, value in yaml.compose(text).value:
        if key.value in ("claims", "debt") and isinstance(value, yaml.SequenceNode):
            for n, item in enumerate(value.value):
                spans[(key.value, n)] = _span(lines, item.start_mark.line, item.end_mark)
        elif key.value in ("meta", "edges"):
            spans[key.value] = _span(lines, key.start_mark.line, value.end_mark)
    used = {n for start, end in spans.values() for n in range(start, end)}
    comments = [f"{STATUS}:{n + 1} {line.strip()}" for n, line in enumerate(lines)
                if n not in used and line.lstrip().startswith("#")]
    return {key: lines[start:end] for key, (start, end) in spans.items()}, comments


def _dedent(block: list[str]) -> list[str]:
    cut = _indent(block[0])
    return [(line[cut:] if not line[:cut].strip() else line.lstrip()) if line.strip() else "" for line in block]


def entry_text(block: list[str], expected: dict, observed: datetime.date | None) -> tuple[list[str], bool]:
    """One claim or debt entry at two spaces, observed: added; False and the
    safe_dump of expected when the original text does not read back to it."""
    body = _dedent(block)
    if observed is not None:
        body.append(" " * (len(body[0]) - len(body[0][1:].lstrip())) + f"observed: {observed}")
    try:
        same = yaml.safe_load("\n".join(body)) == [expected]
    except yaml.YAMLError:
        same = False
    if not same:
        body = yaml.safe_dump([expected], sort_keys=False, allow_unicode=True).splitlines()
    return [f"  {line}" if line else "" for line in body], same


def top_text(block: list[str], key: str, value) -> tuple[list[str], bool]:
    try:
        same = yaml.safe_load("\n".join(block)) == {key: value}
    except yaml.YAMLError:
        same = False
    return (block if same else yaml.safe_dump({key: value}, sort_keys=False, allow_unicode=True).splitlines()), same


def render_area(area: str, sources: list[str] | None, entries: list[list[str]]) -> str:
    head = {"area": area, **({"sources": sources} if sources else {})}
    return (yaml.safe_dump(head, sort_keys=False, allow_unicode=True) + "claims:\n"
            + "\n".join("\n".join(entry) + "\n" for entry in entries))


def claim_paths(claim: dict) -> list[str]:
    if claim.get("kind") == "out_of_repo":
        return []
    raw = claim.get("path")
    paths = [raw] if isinstance(raw, str) else raw if isinstance(raw, list) else []
    normal = [Path(os.path.normpath(p)).as_posix() for p in paths if isinstance(p, str) and p]
    return [p for p in normal if p != "."]


def _slug(prefix: str, taken: set[str]) -> str:
    base = re.sub(r"[^a-z0-9._-]+", "-", prefix.lower()).strip("-._") or "area"
    slug, n = base, 2
    while slug in taken:
        slug, n = f"{base}-{n}", n + 1
    taken.add(slug)
    return slug


def _prefixes(paths: list[str], depth: int, max_claims: int, min_claims: int) -> list[str]:
    """Directory prefixes: a group over max_claims splits by its next path
    segment down to depth; a subgroup under min_claims stays in its parent."""
    def split(prefix: tuple, group: list[tuple]) -> list[str]:
        out = ["/".join(prefix)]
        if len(group) <= max_claims or len(prefix) >= depth:
            return out
        kids = collections.defaultdict(list)
        for segs in group:
            if len(segs) > len(prefix):
                kids[segs[:len(prefix) + 1]].append(segs)
        for kid in sorted(kids):
            if len(kids[kid]) >= min_claims:
                out += split(kid, kids[kid])
        return out

    top = collections.defaultdict(list)
    for path in paths:
        segs = tuple(path.split("/"))
        top[segs[:1]].append(segs)
    return [prefix for key in sorted(top) for prefix in split(key, top[key])]


def _best(path: str, areas: list[tuple[str, list[str]]]) -> str | None:
    """The area whose source covers path with the longest pattern; the first such area on a tie."""
    hits = [(len(source), -n, aid) for n, (aid, sources) in enumerate(areas)
            for source in sources if _matches(path, source)]
    return max(hits)[2] if hits else None


def assign(claims: list[dict], model_areas: list, args) -> tuple[dict, dict]:
    """area id -> claim indexes, and area id -> sources for the shard (None
    when model.yaml has the area)."""
    paths = [claim_paths(c) for c in claims]
    groups: dict[str, list[int]] = collections.defaultdict(list)
    taken = {aid for aid, _ in model_areas} | {GLOBAL}
    sources: dict[str, list[str] | None] = {aid: None for aid in taken}
    if args.by == "component":
        ids: dict[str, str] = {}
        for n, claim in enumerate(claims):
            comp = claim.get("component")
            if not paths[n] or not isinstance(comp, str) or not comp.strip():
                groups[GLOBAL].append(n)
                continue
            aid = ids.setdefault(comp, _slug(comp, taken))
            groups[aid].append(n)
            if aid not in {a for a, _ in model_areas}:
                sources[aid] = sorted(set(sources.get(aid) or []) | set(paths[n]))
        return groups, sources
    candidates = list(model_areas)
    loose = [p[0] for p in paths if p and not any(_best(x, model_areas) for x in p)]
    for prefix in _prefixes(loose, args.depth, args.max_claims, args.min_claims):
        aid = _slug(prefix, taken)
        candidates.append((aid, [prefix]))
        sources[aid] = [prefix]
    for n, claim_path in enumerate(paths):
        hits = [aid for aid in (_best(p, candidates) for p in claim_path) if aid]
        if not hits:
            groups[GLOBAL].append(n)
            continue
        counts = collections.Counter(hits)
        tied = sorted(aid for aid, k in counts.items() if k == max(counts.values()))
        groups[hits[0] if hits[0] in tied else tied[0]].append(n)
    return groups, sources


def model_of(root: Path) -> tuple[Path, str, list] | None:
    """(model root, this repo's id there, its areas as (id, sources)) when a
    model.yaml, here or behind a workspace pointer, describes this repo."""
    if not ((root / gt_store.MODEL).is_file() or (root / WORKSPACE).is_file()):
        return None
    import gt_context

    model_root = gt_context.discover(root)
    model = yaml.safe_load((model_root / gt_store.MODEL).read_text())
    rid = next((rid for rid, spec in (model.get("repositories") or {}).items()
                if (model_root / spec["path"]).resolve() == root.resolve()), None)
    if rid is None:
        raise ContractError(f"{model_root / gt_store.MODEL} has no repository at {root}")
    areas = [(a["id"], a.get("sources") or []) for a in model.get("areas") or [] if a.get("repo") == rid]
    return model_root, rid, areas


def drop_status_source(text: str, rid: str) -> tuple[str, list[str]]:
    """model.yaml text without STATUS.yaml in the sources of this repo's
    areas, and the ids of those areas. The file is gone after the move and
    the shards under .ground-truth/ are never a model source."""
    lines = text.splitlines(keepends=True)
    expected = yaml.safe_load(text)
    drop, ids = set(), []
    for key, value in yaml.compose(text).value:
        if key.value != "areas":
            continue
        for node, area in zip(value.value, expected["areas"]):
            fields = {k.value: v for k, v in node.value}
            if area.get("repo") != rid or "sources" not in fields:
                continue
            hits = [item for item in fields["sources"].value if item.value.removeprefix("./") == STATUS]
            if hits:
                drop |= {item.start_mark.line for item in hits}
                ids.append(area["id"])
                area["sources"] = [s for s in area["sources"] if s.removeprefix("./") != STATUS]
                if not area["sources"]:
                    raise ContractError(f"model area {area['id']} has {STATUS} as its only source; give it another")
    new = "".join(line for n, line in enumerate(lines) if n not in drop)
    if ids and yaml.safe_load(new) != expected:
        raise ContractError(f"cannot drop {STATUS} from the sources of {ids} in model.yaml; remove it by hand")
    return new, ids


def build(root: Path, args) -> dict:
    """Everything --write would do, computed without touching the disk."""
    text = (root / STATUS).read_text()
    data = gt_store.load(root).data
    if set(data) - LEGACY_KEYS:
        raise ContractError(f"{STATUS}: top-level keys the sharded layout has no place for: "
                            f"{sorted(set(data) - LEGACY_KEYS)}")
    claims, debt = data.get("claims") or [], data.get("debt") or []
    ids = [c.get("id") if isinstance(c, dict) else None for c in claims]
    if not isinstance(debt, list) or not all(isinstance(i, str) for i in ids) or len(set(ids)) != len(ids):
        raise ContractError(f"{STATUS}: claims need unique string ids and debt must be a list")
    blocks, comments = legacy_blocks(text)
    dates = contract_lib.legacy_live_state_dates(root, data)
    observed = {c["id"]: datetime.date.fromisoformat(dates.get(c["id"], datetime.date.today().isoformat()))
                for c in claims if c.get("kind") == "live_state"}
    model = model_of(root)
    groups, sources = assign(claims, model[2] if model else [], args)
    warns, files = [], {}

    def keep(lines: list[str], same: bool, what: str) -> list[str]:
        if not same:
            warns.append(f"{what}: text does not read back the same; written with safe_dump")
        return lines

    contract = ["schema_version: 2"]
    for key in ("meta", "edges"):
        if key in data:
            contract += ["", *keep(*top_text(blocks[key], key, data[key]), key)]
    files[gt_store.META] = "\n".join(contract) + "\n"
    kept_debt = [(n, d) for n, d in enumerate(debt)
                 if not (args.drop_resolved and isinstance(d, dict) and d.get("state") == "resolved")]
    if "debt" in data:
        entries = [keep(*entry_text(blocks[("debt", n)], d, None), f"debt #{n + 1}") for n, d in kept_debt]
        files[gt_store.DEBT] = "debt:\n" + "\n".join("\n".join(e) + "\n" for e in entries) if entries else "debt: []\n"
    expected = {}
    for aid in sorted(groups):
        entries = []
        for n in groups[aid]:
            claim = claims[n]
            want = {**claim, "observed": observed[claim["id"]]} if claim["id"] in observed else claim
            expected[claim["id"]] = want
            entries.append(keep(*entry_text(blocks[("claims", n)], want, observed.get(claim["id"])),
                                f"claim {claim['id']}"))
        files[f"{gt_store.AREAS}/{aid}.yaml"] = render_area(aid, sources.get(aid), entries)
    model_edit = None
    if model:
        model_path = model[0] / gt_store.MODEL
        new_text, touched = drop_status_source(model_path.read_text(), model[1])
        if touched:
            model_edit = (model[0], model_path, new_text, touched, fresh_areas(model[0], touched, warns))
    return {"data": data, "files": files, "groups": groups, "sources": sources, "observed": observed,
            "dates": dates, "warns": warns, "comments": comments, "model": model_edit, "expected": expected,
            "debt": [d for _, d in kept_debt] if "debt" in data else None, "dropped": len(debt) - len(kept_debt)}


def fresh_areas(model_root: Path, areas: list[str], warns: list[str]) -> list[str]:
    """The areas with a fresh receipt now: only those get one after the move."""
    import gt_context

    try:
        view = gt_context.inspect(model_root)
    except ValueError as exc:
        warns.append(f"model.yaml: {exc}; no receipt is refreshed")
        return []
    fresh = [a for a in areas if view["areas"][a]["fresh"]]
    warns += [f"model area {a} is not fresh now; its receipt is left as it is" for a in areas if a not in fresh]
    return fresh


def report(root: Path, plan: dict) -> None:
    groups, sources = plan["groups"], plan["sources"]
    width = max(map(len, groups), default=0) + 1
    print(f"{'area':{width}} {'claims':>6}  sources")
    for aid in sorted(groups):
        mark = "  OVERSIZED" if len(groups[aid]) > OVERSIZED else ""
        where = ", ".join(sources[aid]) if sources.get(aid) else "-" if aid == GLOBAL else "(model.yaml)"
        print(f"{aid:{width}} {len(groups[aid]):6}  {where}{mark}")
    states = collections.Counter(d.get("state", "?") for d in plan["data"].get("debt") or [] if isinstance(d, dict))
    print(f"claims in {GLOBAL}: {len(groups.get(GLOBAL, []))}; debt: "
          + (", ".join(f"{k} {v}" for k, v in sorted(states.items())) or "none")
          + (f"; {plan['dropped']} resolved dropped" if plan["dropped"] else ""))
    for cid, day in plan["observed"].items():
        print(f"live_state {cid}: observed {day}" + ("" if cid in plan["dates"] else " (git has no date, today)"))
    print("create: " + ", ".join(plan["files"]))
    print(f"delete: {STATUS}")
    if plan["model"]:
        print(f"model.yaml: drop source {STATUS} from {', '.join(plan['model'][3])}; "
              f"receipts to refresh: {', '.join(plan['model'][4]) or 'none'}")
    for line in plan["comments"]:
        print(f"comment not carried over: {line}")
    for line in plan["warns"]:
        print(f"WARN {line}")
    listed = subprocess.run(["git", "grep", "-l", "-F", STATUS], cwd=root, capture_output=True, text=True)
    # Installed tools read either layout; a repo's own probes may not, and verify runs them.
    by_hand = [p for p in listed.stdout.splitlines()
               if p != STATUS and os.path.dirname(p) != "tools/ground_truth"]
    if by_hand:
        print(f"still name {STATUS}, edit by hand: {', '.join(by_hand)}")


def _norm(issues) -> collections.Counter:
    return collections.Counter((i.severity, i.claim_id, _CONTRACT_REF.sub("<contract>", i.message)) for i in issues)


def equivalence(root: Path, plan: dict, before: collections.Counter) -> tuple[list[str], collections.Counter, collections.Counter]:
    """E1 differences, and the E2 findings only before / only after."""
    legacy = plan["data"]
    try:
        after = gt_store.load(root).data
        e2 = _norm(contract_lib.run(root, "full", execute=False))
    except ContractError as exc:
        return [f"new layout does not load: {exc}"], collections.Counter(), collections.Counter()
    e1 = [key for key in ("meta", "edges") if after.get(key) != legacy.get(key)]
    if after.get("debt") != plan["debt"]:
        e1.append("debt")
    got = {c.get("id"): c for c in after.get("claims") or [] if isinstance(c, dict)}
    want = plan["expected"]
    if len(after.get("claims") or []) != len(want):
        e1.append(f"claims: {len(after.get('claims') or [])} after, {len(want)} before")
    e1 += [f"claim {cid}" for cid in sorted(set(got) | set(want), key=str) if got.get(cid) != want.get(cid)]
    return e1, before - e2, e2 - before


def write(root: Path, plan: dict) -> int:
    status_bytes = (root / STATUS).read_bytes()
    before = _norm(contract_lib.run(root, "full", execute=False))
    new_dirs = [d for d in (root / gt_store.DIR, root / gt_store.AREAS) if not d.exists()]
    created: list[Path] = []
    model = plan["model"]
    model_bytes = model[1].read_bytes() if model else None

    def rollback() -> None:
        for path in created:
            path.unlink(missing_ok=True)
        for d in reversed(new_dirs):
            if d.is_dir() and not any(d.iterdir()):
                d.rmdir()
        (root / STATUS).write_bytes(status_bytes)
        if model:
            model[1].write_bytes(model_bytes)

    try:
        for rel, text in plan["files"].items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            created.append(path)
            path.write_text(text)
        if model:
            model[1].write_text(model[2])
        (root / STATUS).unlink()
        e1, lost, gained = equivalence(root, plan, before)
    except BaseException:
        rollback()
        raise
    fails = sum(v for (sev, _, _), v in (lost + gained).items() if sev == "fail")
    warns = sum(v for (sev, _, _), v in (lost + gained).items() if sev != "fail")
    print(f"data: {'same' if not e1 else 'DIFFERENT: ' + '; '.join(e1)}")
    print(f"equivalent: FAIL Δ{fails}, WARN Δ{warns}")
    if e1 or lost or gained:
        for (sev, cid, msg), _ in sorted(lost.items()):
            print(f"  - [{sev.upper()}] {cid} : {msg}")
        for (sev, cid, msg), _ in sorted(gained.items()):
            print(f"  + [{sev.upper()}] {cid} : {msg}")
        rollback()
        print(f"rolled back: {STATUS} restored, new files removed")
        return 1
    print(f"wrote {len(created)} file(s), deleted {STATUS}")
    # The deletion has to reach the index before model receipts stop seeing STATUS.yaml.
    staged = subprocess.run(["git", "add", "-A", "--", STATUS, gt_store.META, gt_store.DEBT, gt_store.AREAS],
                            cwd=root, capture_output=True, text=True)
    print("staged the contract change; commit it as one change" if staged.returncode == 0
          else f"WARN git add failed: {staged.stderr.strip()}")
    if model and model[4]:
        import gt_context

        try:
            revision = gt_context.inspect(model[0])["revision"]
            gt_context.refresh(model[0], model[4], revision, f"gt migrate: source {STATUS} dropped")
            print(f"receipts refreshed: {', '.join(model[4])}")
        except (OSError, ValueError) as exc:
            print(f"WARN receipts not refreshed: {exc}")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="gt migrate", description=__doc__.splitlines()[0])
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--by", choices=["path", "component"], default="path")
    parser.add_argument("--depth", type=int, default=3)
    parser.add_argument("--max-claims", type=int, default=25)
    parser.add_argument("--min-claims", type=int, default=3)
    parser.add_argument("--drop-resolved", action="store_true")
    parser.add_argument("--skill", type=Path, help="skill directory; installed tools must equal its templates")
    parser.add_argument("--root", type=Path)
    args = parser.parse_args(argv)
    if min(args.depth, args.max_claims, args.min_claims) < 1:
        parser.error("--depth, --max-claims and --min-claims must be at least 1")
    root = (args.root or gt_store.find_root(Path(__file__).resolve().parent) or Path.cwd()).resolve()
    try:
        layout = gt_store.detect_layout(root)
        if layout == gt_store.SHARDED:
            print(f"already migrated ({gt_store.META})")
            return 0
        if layout == gt_store.NONE:
            raise ContractError(f"no {STATUS} at {root}")
        if (root / gt_store.DEBT).exists() or list((root / gt_store.AREAS).glob("*.yaml")):
            raise ContractError(f"{gt_store.DIR} already holds debt or area files; remove them first")
        dirty = subprocess.run(["git", "status", "--porcelain", "--", STATUS, gt_store.DIR],
                               cwd=root, capture_output=True, text=True)
        changed = [l for l in dirty.stdout.splitlines() if gt_store.is_contract_file(l[3:].strip('"'))]
        if dirty.returncode or changed:
            raise ContractError(f"commit the contract first: {dirty.stderr.strip() or ', '.join(changed)}")
        if args.skill:
            drift = gt_doctor.tool_drift(root, args.skill)
            if drift:
                raise ContractError(f"tools/ground_truth differs from the skill templates ({', '.join(drift)}); "
                                    "run i9_install --write first")
            print("tools: same as the skill templates")
        else:
            print("tools: not compared with the skill templates (pass --skill DIR)")
        plan = build(root, args)
    except (ContractError, ValueError, yaml.YAMLError) as exc:
        print(f"gt migrate: {exc}", file=sys.stderr)
        return 3
    report(root, plan)
    if not args.write:
        print("dry run: nothing written; run with --write to apply")
        return 0
    return write(root, plan)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
