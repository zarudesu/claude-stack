#!/usr/bin/env python3
"""Build per-component slices for I.2 from the I.1 research JSON files.

Reads claims.json{claims_found}, debt-candidates.json{suspects} and
every codemap-*.json{components} from run.research, assigns each claim
and debt suspect to the component(s) whose scope covers its path, and
writes one JSON file per component under run.research/slices/ plus a
slices-index.json summary.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from gt_lib import paths  # noqa: E402
from gt_lib.paths import load_run  # noqa: E402

_LINE_SUFFIX_RE = re.compile(r":\d+(-\d+)?$")


def norm(run, p: str) -> str:
    """Absolute path under run.repo -> repo-relative; strip a trailing :N or :N-M; strip slashes."""
    p = (p or "").strip()
    if not p:
        return p
    try:
        p = paths.rel(run, p)
    except ValueError:
        pass
    p = _LINE_SUFFIX_RE.sub("", p)
    return p.strip("/")


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def is_ci_config(ep: str) -> bool:
    """True for a pipeline config file whose named jobs are treated as components."""
    if ep == ".gitlab-ci.yml":
        return True
    if ep.startswith(".github/workflows/") and (ep.endswith(".yml") or ep.endswith(".yaml")):
        return True
    if ep in (".circleci/config.yml", "azure-pipelines.yml"):
        return True
    return False


def scope_of(c: dict) -> str:
    ep = c["entry_point"]
    m = re.match(r"^(roles/[^/]+)/", ep)
    if m:
        return m.group(1)
    if is_ci_config(ep):
        return ep + "#" + c["component"]
    return ep


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:60]


def match(path: str, c: dict):
    """Return 'exact' | 'under' | 'dir-hint' | None for path against component c's scope."""
    s = c["_scope"]
    if "#" in s:
        # a CI-job-style scope matches only by explicit job name, never by bare file
        return None
    if path == s:
        return "exact"
    if path.startswith(s + "/"):
        return "under"
    if s.startswith(path + "/"):
        return "dir-hint"
    if path in c.get("existing_tests", []):
        return "under"
    return None


def assign(comps: list[dict], path: str, extra_text: str = ""):
    hits = []
    for c in comps:
        m = match(path, c)
        if m:
            hits.append((m, c))
        elif "#" in c["_scope"]:
            job = c["_scope"].split("#", 1)[1].split(": ", 1)[-1]
            if job in extra_text or job in path:
                hits.append(("exact", c))
    strong = [c for m, c in hits if m in ("exact", "under")]
    if strong:
        return strong, False
    weak = [c for m, c in hits if m == "dir-hint"]
    if len(weak) > 2:
        return [], True  # too broad: goes to an area bucket instead of fanning out
    return weak, True


def build(run, dry_run: bool) -> dict:
    claims = (load_json(run.research / "claims.json") or {}).get("claims_found", [])
    debt = (load_json(run.research / "debt-candidates.json") or {}).get("suspects", [])

    comps = []
    for cm_path in sorted(run.research.glob("codemap-*.json")):
        for c in (load_json(cm_path) or {}).get("components", []):
            c = dict(c)
            c["entry_point"] = norm(run, c.get("entry_point", ""))
            c["root"] = norm(run, c.get("root", ""))
            c["existing_tests"] = [norm(run, x) for x in c.get("existing_tests", [])]
            c["importers"] = [norm(run, x) for x in c.get("importers", [])]
            c["_scope"] = scope_of(c)
            c["_slug"] = slugify(c["component"])
            comps.append(c)

    seen: dict[str, int] = defaultdict(int)
    for c in comps:
        seen[c["_slug"]] += 1
        if seen[c["_slug"]] > 1:
            c["_slug"] = f"{c['_slug']}-{seen[c['_slug']]}"

    slices = {
        c["_slug"]: {
            "codemap": {k: v for k, v in c.items() if not k.startswith("_")},
            "scope": c["_scope"],
            "claims": [],
            "debt_suspects": [],
        }
        for c in comps
    }
    unmapped_claims, unmapped_debt = [], []

    for cl in claims:
        hint = norm(run, cl.get("hint", ""))
        cands, shared = assign(comps, hint, cl.get("claim", "") + " " + cl.get("quote", ""))
        if not cands and " " in hint:
            for part in re.split(r"[,\s;]+", hint):
                part = norm(run, part)
                if part:
                    c2, s2 = assign(comps, part)
                    cands += c2
                    shared = shared or s2
        if not cands:
            unmapped_claims.append(cl)
            continue
        for c in cands:
            entry = dict(cl)
            entry["shared"] = shared or len(cands) > 1
            slices[c["_slug"]]["claims"].append(entry)

    for s in debt:
        p = norm(run, s.get("path", ""))
        cands, shared = assign(comps, p)
        if not cands:
            unmapped_debt.append(s)
            continue
        for c in cands:
            entry = dict(s)
            entry["shared"] = shared or len(cands) > 1
            slices[c["_slug"]]["debt_suspects"].append(entry)

    # Unmapped items go into an area bucket keyed by the top two path segments.
    # The bucket's component is the area path itself (e.g. "app/core"), never
    # a sentence like "unmapped items under X" -- a sentence there breaks any
    # downstream code that treats codemap.component as a short identifier.
    buckets: dict[str, dict] = defaultdict(lambda: {"claims": [], "debt_suspects": []})
    for cl in unmapped_claims:
        hint = norm(run, cl.get("hint", "")) or "misc"
        hint = re.split(r"[,\s;]+", hint)[0] or "misc"
        key = "/".join(hint.split("/")[:2])
        buckets[key]["claims"].append(cl)
    for s in unmapped_debt:
        key = "/".join(norm(run, s.get("path", "")).split("/")[:2]) or "misc"
        buckets[key]["debt_suspects"].append(s)
    # A bucket with debt but no claim has no drafter target: the reconcile
    # agent drafts claims, and a claim-less slice comes back empty and reads
    # as a failure. Re-home each such suspect to the slice whose entry point
    # (or bucket key) the suspect's description names (a doc describing a missing
    # module lands on that module's slice); keep the bucket only when no
    # slice matches.
    homes = [
        (slug, sl, sl["codemap"].get("entry_point", "")) for slug, sl in slices.items() if sl["claims"]
    ] + [("unmapped-" + slugify(k), b, k) for k, b in buckets.items() if b["claims"]]
    for key in list(buckets):
        b = buckets[key]
        if b["claims"]:
            continue
        left = []
        for s in b["debt_suspects"]:
            text = f"{s.get('description', '')} {norm(run, s.get('path', ''))}"
            hits = [(slug, sl) for slug, sl, anchor in homes if anchor and len(anchor) > 3 and anchor in text]
            if not hits:
                left.append(s)
                continue
            for slug, sl in hits:
                entry = dict(s)
                entry["shared"] = len(hits) > 1
                entry["rehomed_from"] = key
                sl["debt_suspects"].append(entry)
        b["debt_suspects"] = left
        if left:
            print(f"WARN: {len(left)} debt suspect(s) under {key!r} have no claim-bearing slice to join; kept as a debt-only slice")
        else:
            del buckets[key]
    for key, b in buckets.items():
        slug = "unmapped-" + slugify(key)
        slices[slug] = {
            "codemap": {
                "component": key,
                "entry_point": key,
                "root": key,
                "language": "mixed",
                "existing_tests": [],
                "importers": [],
            },
            "scope": key,
            "claims": b["claims"],
            "debt_suspects": b["debt_suspects"],
        }

    index = []
    out_dir = run.research / "slices"
    for slug, sl in slices.items():
        cm = sl["codemap"]
        p = out_dir / f"{slug}.json"
        if not dry_run:
            out_dir.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(sl, indent=1, ensure_ascii=False), encoding="utf-8")
        index.append({
            "component": cm["component"],
            "slice_path": str(p),
            "root": cm.get("root", ""),
            "entry_point": cm.get("entry_point", ""),
            "language": cm.get("language", ""),
            "n_claims": len(sl["claims"]),
            "n_debt": len(sl["debt_suspects"]),
        })

    if not dry_run:
        (run.research / "slices-index.json").write_text(
            json.dumps(index, indent=1, ensure_ascii=False), encoding="utf-8"
        )

    return {
        "components": len(comps),
        "slices": len(index),
        "claims": len(claims),
        "unmapped_claims": len(unmapped_claims),
        "debt": len(debt),
        "unmapped_debt": len(unmapped_debt),
        "index": index,
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Build per-component I.2 slices from I.1 research JSON.")
    parser.add_argument("--run", required=True, help="path to run.json")
    parser.add_argument("--dry-run", action="store_true", help="report counts without writing slice files")
    args = parser.parse_args(argv)

    run = load_run(args.run)
    result = build(run, args.dry_run)

    print(
        f"components={result['components']} slices={result['slices']} "
        f"claims={result['claims']} (unmapped {result['unmapped_claims']}) "
        f"debt={result['debt']} (unmapped {result['unmapped_debt']})"
    )
    for a in result["index"]:
        print(f"  {a['n_claims']:3d}c {a['n_debt']:3d}d  {a['component'][:50]:50s} {a['entry_point']}")

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
