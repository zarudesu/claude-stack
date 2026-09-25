#!/usr/bin/env python3
"""Apply recon-review.js verdicts to STATUS.yaml.

Reads the recon-review workflow's result (--results FILE: an object with a
"results" list of {batch, proposals, verdicts}) and, for every upheld,
non-"none" proposal:

1. Basename disambiguation: a bare filename inside new_text/corrected_text
   (something like "policy.py:98" rather than "services/policy.py:98") is
   expanded to a full tracked path when exactly one candidate matches --
   first among paths already cited elsewhere in the same text, then among
   every tracked file in the repository. An ambiguous bare filename is
   reported and left untouched.
2. Apply by action: "rewrite" replaces the note/description field after
   validating every anchor it contains against the working tree; "resolve"
   marks a debt's state resolved, but only when the debt's ref claim is
   status: implemented -- on a stub/partial/absent ref the resolve is
   rejected and logged (the gap it records is in the code, a doc edit does
   not close it; the reviewer is told to rewrite such entries instead);
   "remove" drops the debt entry outright (duplicates only).

raise_candidate proposals are never applied here. Every one the judge
upheld is written to run.research/raise-candidates.json for the raise-vote
step to pick up; status is never changed for a raise from this stage.

STATUS.yaml is backed up to run.scratch/i5/STATUS.pre-recon<pass>.yaml
before any write.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from gt_lib import paths  # noqa: E402
from gt_lib.git import ls_files  # noqa: E402
from gt_lib.paths import load_run  # noqa: E402
from gt_lib.yaml_edit import blocks, get_field, remove_block, set_field  # noqa: E402

ANCHOR_RE = re.compile(
    r"(?<![\w/.-])((?:[\w.-]+/)*[\w.-]+\.(?:py|yml|yaml|md|ini|cfg|js|go|j2|sh|toml|html|txt|example)):(\d+)(?:-(\d+))?"
)
FORBIDDEN_RE = re.compile(r": |#")


def load_results(path: Path) -> dict:
    raw = path.read_text(encoding="utf-8")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find('{"results"')
        if start < 0:
            raise
        data = json.loads(raw[start:])
    if "results" not in data and isinstance(data.get("result"), dict):
        data = data["result"]
    return data


def expand_basenames(text: str, tracked: set) -> tuple[str, bool, list]:
    """Expand a bare filename in text to the one tracked path it matches.

    Prefers a path already cited in full elsewhere in the same text; falls
    back to the full tracked-file list. Returns (new_text, changed, ambiguous).
    """
    local_full = [m.group(1) for m in ANCHOR_RE.finditer(text) if m.group(1) in tracked]
    changed = False
    ambiguous = []

    def rep(m):
        nonlocal changed
        p = m.group(1)
        if p in tracked:
            return m.group(0)
        cands = sorted({f for f in local_full if f == p or f.endswith("/" + p)})
        if len(cands) != 1:
            cands = sorted({f for f in tracked if f == p or f.endswith("/" + p)})
        if len(cands) == 1:
            changed = True
            return m.group(0).replace(p, cands[0], 1)
        if len(cands) > 1:
            ambiguous.append((p, cands))
        return m.group(0)

    out = ANCHOR_RE.sub(rep, text)
    return out, changed, ambiguous


def text_ok(s: str) -> bool:
    if not s or not s.strip():
        return False
    if "\n" in s or FORBIDDEN_RE.search(s) or "CLAUDE.md" in s:
        return False
    return True


def bad_anchor(repo: Path, text: str) -> str | None:
    for m in ANCHOR_RE.finditer(text):
        p, a, b = m.group(1), int(m.group(2)), int(m.group(3) or m.group(2))
        f = repo / p
        if not f.is_file():
            return f"missing file {p}"
        n = len(f.read_text(encoding="utf-8", errors="replace").split("\n"))
        if a < 1 or a > b or b > n:
            return f"{p}:{a}-{b} beyond {n} lines"
    return None


def build(run, status_path: Path, data: dict, skip_ids: set, write: bool, pass_n: int) -> dict:
    t = status_path.read_text(encoding="utf-8")
    E = blocks(t)
    tracked = set(ls_files(run.repo))

    log = {
        "skipped_manual": [], "rewrite": [], "resolve": [], "remove": [],
        "rejected": [], "unjudged": [], "escalate": [], "raise": [],
    }
    raise_candidates = []

    for r in data.get("results") or []:
        verdicts = r.get("verdicts")
        judged = verdicts is not None
        vm = {v["id"]: v for v in (verdicts or [])}
        for p in r.get("proposals") or []:
            i = p["id"]
            if p.get("escalate"):
                log["escalate"].append({"id": i, "escalate": p["escalate"][:300]})
            if p.get("raise_candidate"):
                v = vm.get(i)
                # a raise is always about a claim's status -- for a proposal
                # against a debt entry, record the claim it references
                # (get_field(t, i, "ref")) rather than the debt's own id, so
                # the id still resolves in STATUS.yaml even after this same
                # run resolves or removes the debt below.
                ref_id = get_field(t, i, "ref") if i in E else None
                claim_id = ref_id or i
                entry = {
                    "id": claim_id, "raise_reason": p.get("raise_reason", "")[:300],
                    "upheld": v["upheld"] if v else None,
                    "judge_reason": (v or {}).get("reason", "")[:300],
                }
                log["raise"].append(entry)
                if v and v.get("upheld"):
                    raise_candidates.append({"id": claim_id, "raise_reason": entry["raise_reason"], "judge_reason": entry["judge_reason"]})
            if p["action"] == "none":
                continue
            if i in skip_ids:
                log["skipped_manual"].append({"id": i, "action": p["action"]})
                continue
            if i not in E:
                log["rejected"].append({"id": i, "action": p["action"], "reason": "unknown id"})
                continue
            if not judged:
                log["unjudged"].append({"id": i, "action": p["action"]})
                continue
            v = vm.get(i)
            if not v or not v.get("upheld"):
                log["rejected"].append({"id": i, "action": p["action"], "reason": (v or {}).get("reason", "no verdict")[:300]})
                continue

            kind = "debt" if get_field(t, i, "ref") else "claim"

            if p["action"] == "rewrite":
                field = p.get("field") or ("description" if kind == "debt" else "note")
                if (field == "note") != (kind == "claim"):
                    field = "description" if kind == "debt" else "note"
                new = v.get("corrected_text") or p.get("new_text") or ""
                new, _, amb = expand_basenames(new, tracked)
                if amb:
                    log["rejected"].append({"id": i, "action": "rewrite", "reason": f"ambiguous basename {amb}"})
                    continue
                reason = None if text_ok(new) else "text rules"
                reason = reason or bad_anchor(run.repo, new)
                if reason:
                    log["rejected"].append({"id": i, "action": "rewrite", "reason": reason})
                    continue
                if write:
                    t = set_field(t, i, field, new)
                log["rewrite"].append({"id": i, "field": field, "source": "corrected" if v.get("corrected_text") else "as proposed"})
            elif p["action"] in ("resolve", "remove"):
                if kind != "debt":
                    log["rejected"].append({"id": i, "action": p["action"], "reason": "not a debt entry"})
                    continue
                ref = get_field(t, i, "ref")
                rs = (get_field(t, ref, "status") or "").strip() if ref and ref in E else None
                if p["action"] == "resolve" and rs == "implemented":
                    if write:
                        t = set_field(t, i, "state", "resolved")
                    log["resolve"].append({"id": i, "ref": ref, "ref_status": rs})
                elif p["action"] == "resolve":
                    # a debt on a stub/partial/absent claim records a gap in the
                    # code; a doc edit cannot close it, and dropping the entry
                    # would lose the record of the promise -- leave it for a hand
                    # rewrite instead of removing it.
                    log["rejected"].append({"id": i, "action": "resolve", "ref": ref, "ref_status": rs,
                                            "reason": "ref claim is not implemented; rewrite the description, do not resolve"})
                else:
                    if write:
                        t = remove_block(t, i)
                        E.pop(i, None)
                    log["remove"].append({"id": i, "ref": ref, "ref_status": rs, "requested": p["action"]})

    error = None
    if write:
        import yaml

        try:
            yaml.safe_load(t)
        except yaml.YAMLError as exc:
            error = f"recon edits produced invalid YAML; STATUS.yaml left untouched: {exc}"
        else:
            backup = paths.stage_dir(run, "i5") / f"STATUS.pre-recon{pass_n}.yaml"
            backup.write_text(status_path.read_text(encoding="utf-8"), encoding="utf-8")
            status_path.write_text(t, encoding="utf-8")

    raise_path = run.research / "raise-candidates.json"
    if write and error is None:
        existing = []
        if raise_path.is_file():
            existing = json.loads(raise_path.read_text(encoding="utf-8"))
        by_id = {e["id"]: e for e in existing}
        for c in raise_candidates:
            by_id[c["id"]] = c
        raise_path.write_text(json.dumps(list(by_id.values()), indent=1, ensure_ascii=False), encoding="utf-8")

    return {"log": log, "raise_candidates": raise_candidates, "raise_path": str(raise_path), "missing": data.get("missing"), "error": error}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Apply recon-review.js verdicts to STATUS.yaml.")
    parser.add_argument("--run", required=True, help="path to run.json")
    parser.add_argument("--results", required=True, help="path to the recon-review.js result JSON")
    parser.add_argument("--skip-ids", help="file of ids to leave untouched, whitespace-separated")
    parser.add_argument("--status", help="path to STATUS.yaml (default: run.repo/STATUS.yaml)")
    parser.add_argument("--pass", dest="pass_n", type=int, choices=(1, 2), default=1, help="recon pass number, used in the backup filename")
    parser.add_argument("--write", action="store_true", help="write STATUS.yaml and raise-candidates.json")
    parser.add_argument("--dry-run", action="store_true", help="report only, write nothing (default)")
    args = parser.parse_args(argv)

    run = load_run(args.run)
    write = args.write and not args.dry_run

    status_path = Path(args.status) if args.status else run.repo / "STATUS.yaml"
    if not status_path.is_file():
        print(f"error: STATUS.yaml not found at {status_path}", file=sys.stderr)
        return 1

    data = load_results(Path(args.results))
    skip_ids = set()
    if args.skip_ids:
        skip_ids = set(Path(args.skip_ids).read_text(encoding="utf-8").split())

    result = build(run, status_path, data, skip_ids, write, args.pass_n)

    for k, v in result["log"].items():
        print(f"== {k}: {len(v)}")
        for x in v:
            print("  ", x)
    print("missing batches:", result["missing"])
    if result.get("error"):
        print(f"error: {result['error']}", file=sys.stderr)
        return 1
    print(f"raise candidates upheld: {len(result['raise_candidates'])} -> {result['raise_path']}" if write else
          f"raise candidates upheld: {len(result['raise_candidates'])} (dry run, not written)")
    if not write:
        print("dry run: STATUS.yaml not written")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
