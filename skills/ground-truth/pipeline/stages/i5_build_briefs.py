#!/usr/bin/env python3
"""Build the I.5 conflict briefs from claims-judged.json and the I.1 claims.

Writes run.research/i5/docs/<slug>.json for every tracked *.md (except
docs/_human/**) and run.research/i5/code/<slug>.json for every coverage
root, plus run.research/i5/index.json with the counts. Nothing in the
repo is touched; i5_build_args names these files and i5.js reads them.

Sources:
- items[].doc_conflicts (I.2 reconcile, one line per place where a doc
  or a code comment says something the code does not do). A line is
  routed by its first file:line reference: a *.md file -> that doc's
  brief; anything else -> the code brief of the coverage root the file
  sits under. Every line is deduplicated by text and attributed to one
  claim of its component: the claim whose path names a file cited in
  the line, else the claim whose id shares the most words with it, else
  the component's first claim.
- judged[] final_* fields -> final_claims; judged[].debt -> debt_mentions
  (entries whose path or description names the doc).
- research/claims.json claims_found -> i1_claims (sentences extracted
  from that doc by the I.1 claims finder, line numbers of the unedited
  file).
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
from gt_lib.git import ls_files  # noqa: E402
from gt_lib.paths import load_run  # noqa: E402

import yaml  # noqa: E402

FILE_REF_RE = re.compile(r"(?<![\w/])([\w.\-]+(?:/[\w.\-]+)+|[\w\-]+\.\w+)(?::(\d+)(?:-(\d+))?)?")
WORD_RE = re.compile(r"[a-z0-9]+")
STOP_WORDS = {"the", "and", "for", "with", "not", "but", "that", "this", "from", "into", "are", "was", "has", "have", "any", "all", "its", "does", "only", "when", "where"}


def slugify(rel_path: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", rel_path.lower()).strip("_")


def file_refs(text: str) -> list[str]:
    """File paths cited in a conflict line, in order of appearance."""
    out = []
    for m in FILE_REF_RE.finditer(text):
        f = m.group(1)
        if "." in f.rsplit("/", 1)[-1] or "/" in f:
            out.append(f)
    return out


def root_of(path: str, roots: list[str]) -> str | None:
    for r in sorted(roots, key=len, reverse=True):
        if r == "." or path == r or path.startswith(r.rstrip("/") + "/"):
            return r
    return None


def attribute(conflict: str, comp_claims: list[dict], all_claims: list[dict]) -> str:
    """Pick the claim a conflict line belongs to: the one whose path names a
    file the line cites and whose id shares the most words with it; the
    component's own claims win ties; no signal at all -> the component's
    first claim."""
    refs = file_refs(conflict)
    code_refs = [r for r in refs if not r.endswith(".md")]
    words = {w for w in WORD_RE.findall(conflict.lower()) if len(w) > 2 and w not in STOP_WORDS}
    own = {c["claim_id"] for c in comp_claims}

    def score(c):
        paths = set(c.get("final_path") or [])
        # the first code file a line cites is the one it is about; a later
        # citation is supporting context and weighs less
        path_score = sum(3 if r == code_refs[0] else 2 for r in set(refs) & paths) if code_refs else 2 * len(set(refs) & paths)
        return (path_score
                + len(words & set(c["claim_id"].split("_")))
                + (0.5 if c["claim_id"] in own else 0)
                # a doc that overstates contradicts the claim that is not implemented
                + (0.25 if c.get("final_status") in ("stub", "partial", "absent", "design-only") else 0))

    ranked = sorted(all_claims, key=score, reverse=True)
    if ranked and score(ranked[0]) > 0.5:
        return ranked[0]["claim_id"]
    return comp_claims[0]["claim_id"] if comp_claims else ""


def final_entry(j: dict) -> dict:
    return {
        "claim_id": j["claim_id"], "kind": j.get("final_kind"), "status": j.get("final_status"),
        "check": j.get("final_check"), "note": j.get("final_note"),
    }


def build(run, status_path: Path) -> dict:
    judged_doc = json.loads((run.research / "claims-judged.json").read_text(encoding="utf-8"))
    judged = [j for j in judged_doc.get("judged") or [] if not j.get("merged_into")]
    by_id = {j["claim_id"]: j for j in judged}
    items = judged_doc.get("items") or []
    status = yaml.safe_load(status_path.read_text(encoding="utf-8")) or {}
    roots = ((status.get("meta") or {}).get("coverage") or {}).get("roots") or []
    i1 = json.loads((run.research / "claims.json").read_text(encoding="utf-8")).get("claims_found") or []

    # component -> surviving judged claims, in draft order
    comp_claims: dict[str, list[dict]] = defaultdict(list)
    for it in items:
        cid = (it.get("draft") or {}).get("id")
        if cid in by_id and by_id[cid] not in comp_claims[it["component"]]:
            comp_claims[it["component"]].append(by_id[cid])

    doc_conf: dict[str, list[dict]] = defaultdict(list)
    code_conf: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    unrouted: list[str] = []
    seen: set = set()
    for it in items:
        for line in it.get("doc_conflicts") or []:
            text = line.strip()
            if not text:
                continue
            refs = file_refs(text)
            cid = attribute(text, comp_claims.get(it["component"]) or [], judged)
            # two phrasings of the same finding (same cited location, same claim) count once
            m = FILE_REF_RE.search(text)
            key = (m.group(0) if m else text, cid) if refs else text
            if text in seen or key in seen:
                continue
            seen.add(text)
            seen.add(key)
            entry = {"claim_id": cid, "conflict": text}
            if refs and refs[0].endswith(".md"):
                doc_conf[refs[0]].append(entry)
            elif refs and root_of(refs[0], roots):
                code_conf[root_of(refs[0], roots)][refs[0]].append(entry)
            else:
                unrouted.append(text)

    md_files = [p for p in ls_files(run.repo, "*.md") if not p.startswith("docs/_human/")]
    docs_dir, code_dir = paths.research(run, "i5", "docs"), paths.research(run, "i5", "code")
    docs_dir.mkdir(parents=True, exist_ok=True)
    code_dir.mkdir(parents=True, exist_ok=True)

    index = {"docs": [], "areas": [], "unrouted_conflicts": unrouted}
    for doc in sorted(md_files):
        conflicts = doc_conf.get(doc, [])
        i1_claims = [
            {"quote": c.get("quote"), "file": c.get("file"), "line": c.get("line"), "normative_claim_text": c.get("claim"),
             "code_area_hint": c.get("hint"), "source": c.get("source")}
            for c in i1 if c.get("file") == doc
        ]
        debt_mentions = []
        for j in judged:
            for d in j.get("debt") or []:
                where = f"{d.get('path') or ''} {d.get('description') or ''}"
                if doc in where:
                    debt_mentions.append({"claim_id": j["claim_id"], "description": d.get("description"), "severity": d.get("severity")})
        hint_text = " ".join(f"{c.get('code_area_hint') or ''} {c.get('quote') or ''}" for c in i1_claims)
        ids = {c["claim_id"] for c in conflicts} | {d["claim_id"] for d in debt_mentions}
        ids |= {j["claim_id"] for j in judged if any(p and p in hint_text for p in (j.get("final_path") or []))}
        final_claims = [final_entry(by_id[i]) for i in sorted(ids) if i in by_id]
        brief = {"doc": doc, "conflicts": conflicts, "final_claims": final_claims, "i1_claims": i1_claims, "debt_mentions": debt_mentions}
        (docs_dir / f"{slugify(doc)}.json").write_text(json.dumps(brief, indent=1, ensure_ascii=False), encoding="utf-8")
        index["docs"].append({"doc": doc, "slug": slugify(doc), "n_conflicts": len(conflicts), "n_claims": len(final_claims),
                              "n_i1": len(i1_claims), "n_debt": len(debt_mentions)})
    for area in roots:
        files = {f: v for f, v in sorted(code_conf.get(area, {}).items())}
        brief = {"area": area, "files": files}
        (code_dir / f"{slugify(area)}.json").write_text(json.dumps(brief, indent=1, ensure_ascii=False), encoding="utf-8")
        index["areas"].append({"area": area, "slug": slugify(area), "n_files": len(files), "n_conflicts": sum(len(v) for v in files.values())})
    for doc, conflicts in doc_conf.items():
        if doc not in md_files:
            index["unrouted_conflicts"].extend(f"{doc} is not a tracked doc: {c['conflict']}" for c in conflicts)
    (paths.research(run, "i5", "index.json")).write_text(json.dumps(index, indent=1, ensure_ascii=False), encoding="utf-8")
    return index


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Build the I.5 conflict briefs (docs + code areas) from claims-judged.json.")
    parser.add_argument("--run", required=True, help="path to run.json")
    parser.add_argument("--status", help="path to STATUS.yaml (default: run.repo/STATUS.yaml)")
    args = parser.parse_args(argv)
    run = load_run(args.run)
    status_path = Path(args.status) if args.status else run.repo / "STATUS.yaml"
    if not status_path.is_file():
        print(f"error: STATUS.yaml not found at {status_path}", file=sys.stderr)
        return 1
    index = build(run, status_path)
    for d in index["docs"]:
        print(f"doc  {d['doc']}: conflicts={d['n_conflicts']} claims={d['n_claims']} i1={d['n_i1']} debt={d['n_debt']}")
    for a in index["areas"]:
        print(f"area {a['area']}: files={a['n_files']} conflicts={a['n_conflicts']}")
    for u in index["unrouted_conflicts"]:
        print(f"WARN unrouted conflict: {u[:160]}")
    print(f"wrote {paths.research(run, 'i5')}/{{docs,code}}/*.json + index.json")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
