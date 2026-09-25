#!/usr/bin/env python3
"""Turn claims-judged.json into a draft STATUS.yaml plus the side files a human still decides on.

Reads run.research/claims-judged.json, writes STATUS.yaml, escalate.json,
code-fix-candidates.json, canary-candidates.json and assemble-report.json
into --out-dir, then runs the shipped verifier's shape check against that
directory. Component names come straight from claims-judged.json's items
(as i2_build_slices produced them) and are never rewritten into prose.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from gt_lib.banned_words import CLAUDE_MD_RE, scan_identity  # noqa: E402
from gt_lib.paths import load_run  # noqa: E402

import yaml  # noqa: E402


class DQ(str):
    """Marker: dump as a double-quoted YAML scalar."""


yaml.add_representer(
    DQ, lambda d, data: d.represent_scalar("tag:yaml.org,2002:str", str(data), style='"'),
    Dumper=yaml.SafeDumper,
)


def under_human_docs(rel: str) -> bool:
    parts = Path(os.path.normpath(Path(rel).as_posix())).as_posix().split("/")
    return any(parts[i] == "docs" and parts[i + 1] == "_human" for i in range(len(parts) - 1))


def scan_banned(text, cid, field, defects):
    for word, matched in scan_identity(text or ""):
        defects.append({"type": "banned_word", "claim_id": cid, "field": field, "word": word, "matched": matched})


def strip_claude_md_refs(text, cid, defects):
    if not text or not CLAUDE_MD_RE.search(text):
        return text
    defects.append({"type": "note_claude_md_ref", "claim_id": cid})
    return re.sub(r"\s{2,}", " ", CLAUDE_MD_RE.sub("", text)).strip(" ;,")


def set_or_missing(claim, key, value, cid, defects):
    if value:
        claim[key] = value
    else:
        defects.append({"type": "missing_field", "claim_id": cid, "field": key})


def component_map(items):
    out = {}
    for it in items or []:
        cid = (it.get("draft") or {}).get("id")
        if cid and it.get("component"):
            out[cid] = it["component"]
    return out


def dedupe(judged, defects):
    seen, ordered = set(), []
    for j in judged:
        cid = j.get("claim_id")
        if cid in seen:
            defects.append({"type": "duplicate_id", "claim_id": cid})
            continue
        seen.add(cid)
        ordered.append(j)
    return ordered


def build_claim(j, comp_map, merge_notes, repo_root, defects):
    cid = j["claim_id"]
    claim = {"id": cid}

    comp = comp_map.get(cid)
    if comp:
        claim["component"] = comp
        scan_banned(comp, cid, "component", defects)
    else:
        defects.append({"type": "missing_field", "claim_id": cid, "field": "component"})

    kind = j.get("final_kind")
    claim["kind"] = kind

    raw_paths = j.get("final_path") or []
    if kind == "status" and not raw_paths:
        defects.append({"type": "missing_field", "claim_id": cid, "field": "path"})
    paths = []
    for p in raw_paths:
        if under_human_docs(p):
            defects.append({"type": "path_human_docs", "claim_id": cid, "path": p})
            continue
        if not (repo_root / p).exists():
            sub = substitute_path(repo_root, p, j)
            defects.append({"type": "path_missing", "claim_id": cid, "path": p, "substituted": sub})
            if sub and sub not in paths:
                paths.append(sub)
            continue
        paths.append(p)
    if paths:
        claim["path"] = paths

    if kind == "status":
        set_or_missing(claim, "status", j.get("final_status"), cid, defects)
        set_or_missing(claim, "check_kind", j.get("final_check_kind"), cid, defects)
        set_or_missing(claim, "check", j.get("final_check"), cid, defects)
    elif kind == "live_state":
        set_or_missing(claim, "command", j.get("final_command"), cid, defects)
    elif kind == "out_of_repo":
        set_or_missing(claim, "pointer", j.get("final_pointer"), cid, defects)
    else:
        defects.append({"type": "missing_field", "claim_id": cid, "field": "kind"})

    if kind == "status" and j.get("canary") is True and isinstance(j.get("mutation"), dict):
        m = j["mutation"]
        if all(m.get(k) for k in ("file", "find", "replace")):
            claim["canary"] = True
            claim["mutation"] = {k: m[k] for k in ("file", "find", "replace")}
        else:
            defects.append({"type": "canary_incomplete", "claim_id": cid})

    note = j.get("final_note") or ""
    for src in merge_notes.get(cid, []):
        note = (note + "; " if note else "") + f"merged from {src}"
    note = strip_claude_md_refs(note, cid, defects)
    if not note:
        defects.append({"type": "missing_field", "claim_id": cid, "field": "note"})
    scan_banned(note, cid, "note", defects)
    claim["note"] = DQ(note)
    return claim


_CITED_PATH_RE = re.compile(r"(?<![\w/])((?:[\w.-]+/)+[\w.-]+)(?::\d+(?:-\d+)?)?")


def substitute_path(repo_root: Path, missing: str, j: dict) -> str | None:
    """A path that does not exist cannot anchor a claim: the verifier fails
    the exists-check in every mode, with no exemption for absent or
    design-only. The judge names the missing file itself for those; anchor
    the claim instead at the first existing file the judge cited (debt
    entries first, since they point at the promise, then the note), else
    at the nearest existing ancestor directory."""
    texts = [f"{d.get('path') or ''} {d.get('description') or ''}" for d in j.get("debt") or []]
    texts.append(j.get("final_note") or "")
    for text in texts:
        for m in _CITED_PATH_RE.finditer(text):
            cand = m.group(1).rstrip(".,;")
            if cand != missing and not under_human_docs(cand) and (repo_root / cand).is_file():
                return cand
    parent = Path(missing).parent
    while parent.as_posix() not in ("", "."):
        if (repo_root / parent).is_dir():
            return parent.as_posix()
        parent = parent.parent
    return None


def build(judged, items, repo_root, audited, defects):
    comp_map = component_map(items)
    ordered = dedupe(judged, defects)

    merge_notes, emit_ids = {}, set()
    for j in ordered:
        target = j.get("merged_into") or ""
        if target == j["claim_id"]:
            defects.append({"type": "merged_into_self", "claim_id": target})
            target = ""
        if target:
            merge_notes.setdefault(target, []).append(j["claim_id"])
        elif j.get("route") != "ESCALATE":
            emit_ids.add(j["claim_id"])

    merged_list = []
    for target, sources in merge_notes.items():
        merged_list += [{"claim_id": s, "merged_into": target} for s in sources]
        if target not in emit_ids:
            defects.append({"type": "merge_target_missing", "claim_id": target, "sources": sources})

    claims, debt = [], []
    for j in ordered:
        cid = j["claim_id"]
        if cid not in emit_ids:
            continue
        claims.append(build_claim(j, comp_map, merge_notes, repo_root, defects))
        for i, d in enumerate(j.get("debt") or []):
            desc = d.get("description", "")
            dpath = (d.get("path") or "").strip()
            if dpath and dpath.split(":", 1)[0] not in desc:
                desc = f"{dpath} -- {desc}"
            scan_banned(desc, cid, "debt.description", defects)
            debt.append({
                "id": f"{cid}_debt_{i + 1}", "ref": cid, "description": desc,
                "severity": d.get("severity"), "state": "open", "owner": "", "opened": audited,
            })

    claims.sort(key=lambda c: c["id"])
    debt.sort(key=lambda d: d["id"])
    return claims, debt, merged_list


def side_files(judged):
    escalate = [j for j in judged if j.get("route") == "ESCALATE" and not j.get("merged_into")]
    code_fix = [
        {"claim_id": j["claim_id"], "final_status": j.get("final_status"),
         "code_fix_brief": j.get("code_fix_brief"), "reason": j.get("reason")}
        for j in judged if j.get("route") == "CODE_FIX_CANDIDATE" and not j.get("merged_into")
    ]
    canary = [
        {"claim_id": j["claim_id"], "mutation_suggestion": j.get("mutation_suggestion")}
        for j in judged
        if j.get("canary_candidate") and j.get("final_kind") == "status"
        and j.get("route") != "ESCALATE" and not j.get("merged_into")
    ]
    return escalate, code_fix, canary


def counts_of(claims, judged):
    counts = {"kind": {}, "status": {}, "route": {}}
    for c in claims:
        counts["kind"][c["kind"]] = counts["kind"].get(c["kind"], 0) + 1
        if "status" in c:
            counts["status"][c["status"]] = counts["status"].get(c["status"], 0) + 1
    for j in judged:
        if j.get("route"):
            counts["route"][j["route"]] = counts["route"].get(j["route"], 0) + 1
    return counts


def validate(out_dir: Path, templates_dir: Path) -> None:
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(templates_dir))
    import contract_lib

    print(f'VALIDATION COMMAND: contract_lib.check_shape(Path("{out_dir}"), '
          f'contract_lib.load_contract(Path("{out_dir}")))')
    issues = contract_lib.check_shape(out_dir, contract_lib.load_contract(out_dir))
    if not issues:
        print("VALIDATION RESULT: OK (0 issues)")
    for iss in issues:
        print(f"VALIDATION RESULT: [{iss.severity.upper()}] {iss.claim_id} : {iss.message}")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Turn claims-judged.json into a draft STATUS.yaml plus the side files a human still decides on.",
    )
    parser.add_argument("--run", required=True, help="path to run.json")
    parser.add_argument("--repo-name", required=True, help="repo display name for STATUS.yaml meta.repo")
    parser.add_argument("--roots", required=True, help="comma-separated coverage root paths")
    parser.add_argument("--runner", default="none", help="coverage runner name for STATUS.yaml meta")
    parser.add_argument("--out-dir", help="output directory (default: run.scratch/i35)")
    parser.add_argument("--audited", help="last_audited date, YYYY-MM-DD (default: run.date)")
    args = parser.parse_args(argv)

    run = load_run(args.run)
    out_dir = Path(args.out_dir) if args.out_dir else run.scratch / "i35"
    out_dir.mkdir(parents=True, exist_ok=True)
    audited = args.audited or run.date

    judged_path = run.research / "claims-judged.json"
    data = json.loads(judged_path.read_text(encoding="utf-8"))
    judged, items = data.get("judged") or [], data.get("items") or []

    defects = []
    claims, debt, merged_list = build(judged, items, run.repo, audited, defects)

    doc = {
        "schema_version": 1,
        "meta": {
            "repo": args.repo_name,
            "enforcement": "advisory",
            "last_audited": audited,
            "coverage": {
                "roots": [r.strip() for r in args.roots.split(",") if r.strip()],
                "runner": args.runner,
            },
        },
        "claims": claims,
        "debt": debt,
        "edges": [],
    }
    yaml_text = yaml.dump(doc, Dumper=yaml.SafeDumper, sort_keys=False, allow_unicode=True, width=1000)
    (out_dir / "STATUS.yaml").write_text(yaml_text, encoding="utf-8")

    escalate, code_fix, canary = side_files(judged)
    (out_dir / "escalate.json").write_text(json.dumps(escalate, indent=2, ensure_ascii=False), encoding="utf-8")
    (out_dir / "code-fix-candidates.json").write_text(json.dumps(code_fix, indent=2, ensure_ascii=False), encoding="utf-8")
    (out_dir / "canary-candidates.json").write_text(json.dumps(canary, indent=2, ensure_ascii=False), encoding="utf-8")
    report = {"counts": counts_of(claims, judged), "merged_claims": merged_list, "defects": defects}
    (out_dir / "assemble-report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"wrote {out_dir}/STATUS.yaml claims={len(claims)} debt={len(debt)} defects={len(defects)}")
    validate(out_dir, run.skill / "templates")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
