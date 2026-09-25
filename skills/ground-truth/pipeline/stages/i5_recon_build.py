#!/usr/bin/env python3
"""Build recon batches: STATUS.yaml records that still need a human/agent
look after templates/remap_line_refs.py has run.

remap_line_refs.py rewrites the citations it can parse with confidence
(an adjacent or detached path:N) and leaves everything else alone: a
citation into now-deleted lines, an association-ambiguous detached or
prose citation, and (with --pre) a prose citation it judged safe to skip.
Its --report JSON is the primary input here: every id in its `deleted`,
`unparsed` and `assoc_risk` lists is selected outright -- an unparsed
citation is exactly the case remap_line_refs.py gave up on, so it needs
a human look at least as much as one it rewrote -- and so is every
`rewritten` entry with kind "replaced" -- that kind already means the
citation's old line fell inside a hunk that changed content, so the
record needs a look
regardless of how similar the old and new text happen to read. A
SequenceMatcher ratio between the old and new line text only chooses how
urgent the flag is: substantive (ratio < 0.3), low_sim (ratio < 0.6), or
replaced (ratio >= 0.6, still selected -- a short but meaningful edit can
score a deceptively high ratio, which is why an earlier ratio-only cutoff
here needed a hand-picked list of exceptions).

Independently of the report, this also re-scans the current STATUS.yaml
text itself for prose citations ("line N", "lines N-M") and for entries
naming two or more modified paths together with a detached or prose
citation -- catching cases the report's own --pre guard chose not to
touch, or a run made without --report at all. The prose scan runs per
note/description/why/reason/evidence value, the same values
remap_line_refs.py rewrites: a "line N" is attributed to the nearest
earlier path mention in that value, modified or not, and flagged only
when that path is modified. A value whose nearest path is untouched is
left alone even when another field of the same entry (check:, path:)
names a modified file -- scanning the whole block used to hand "line 6"
of an unchanged app/export.py note to the modified test file named in
check:. A prose match is only flagged when --pre is also given and its
25-character leading context still matches the pre-edit block for that
id (same guard remap_line_refs itself uses); without --pre this scan is
skipped and a note is printed, since an ungated prose scan on its own
produces too many false positives to be useful.

A third scan needs no citation at all: a note or description that quotes
a line the edit pass removed -- five or more consecutive words of a
deleted diff line, compared case-insensitively with punctuation dropped
-- is flagged quoted_removed. This is the drift the anchor scans cannot
see: a debt that says "despite the module docstring claiming both
functions produce a full export" cites app/export.py:17-18, outside the
docstring hunk that rewrote exactly that sentence, so no line number
moves and nothing above selects it, yet its wording is now false.

Flags: low_sim, deleted, substantive, assoc_risk, prose_far, replaced,
unparsed, quoted_removed.

--pass 1 selects every id that picked up a flag. --pass 2 re-scans the
same way but is scoped to exactly the ids named in --unbatched (a JSON
list of ids), typically the ones a first recon-review round returned as
missing or unresolved -- unbatched ids that pick up no flag on rescan are
still included, since something already flagged them for a second look.

Batches: ids with any flag besides bare assoc_risk are "heavy" (9 per
batch), assoc_risk-only ids are "light" (14 per batch) -- lighter
because a reviewer only has to pick the one intended path, not judge a
rewrite. Pass 1 writes to run.scratch/recon/, pass 2 to
run.scratch/recon/pass2/.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from difflib import SequenceMatcher
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from gt_lib import paths  # noqa: E402
from gt_lib.git import diff_names, head_lines, hunks, removed_lines  # noqa: E402
from gt_lib.paths import load_run  # noqa: E402
from gt_lib.yaml_edit import blocks, get_field  # noqa: E402

HEAVY_BATCH_SIZE = 9
LIGHT_BATCH_SIZE = 14
LOW_SIM_THRESHOLD = 0.6
SUBSTANTIVE_THRESHOLD = 0.3


def _load_remap_module(skill: Path):
    """Import templates/remap_line_refs.py by path (it is not a package)."""
    spec = importlib.util.spec_from_file_location("remap_line_refs", skill / "templates" / "remap_line_refs.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _span(s: str) -> tuple[int, int]:
    """Pull the first one or two digit-groups out of a citation span's text.

    The remap report's old/new fields are the original matched text, not a
    bare number (":10-12", " lines 10-12", " (lines 10-12)"), so this reads
    the digits out with a regex rather than assuming a plain "N" or "N-M".
    """
    nums = re.findall(r"\d+", s)
    if not nums:
        raise ValueError(f"no line number found in {s!r}")
    if len(nums) == 1:
        return int(nums[0]), int(nums[0])
    return int(nums[0]), int(nums[-1])


def _line_range(lines: list[str], lo: int, hi: int) -> str:
    return "\n".join(lines[lo - 1:hi])


class ItemSet:
    def __init__(self):
        self.items: dict[str, dict] = {}

    def add(self, item_id: str, flag: str, cite: dict | None = None) -> None:
        it = self.items.setdefault(item_id, {"flags": set(), "cites": []})
        it["flags"].add(flag)
        if cite is not None:
            it["cites"].append(cite)


def from_report(items: ItemSet, report: dict, repo: Path) -> None:
    for d in report.get("deleted", []):
        items.add(d["id"], "deleted", {"path": d["path"], "text": d["text"], "source": "remap_deleted"})
    for u in report.get("assoc_risk", []):
        items.add(u["id"], "assoc_risk", {"paths": u.get("paths", []), "text": u["text"], "source": "remap_assoc_risk"})
    for u in report.get("unparsed", []):
        items.add(u["id"], "unparsed", {"path": u.get("path"), "text": u["text"], "source": "remap_unparsed"})
    for r in report.get("rewritten", []):
        if r.get("kind") != "replaced":
            continue
        try:
            old_lo, old_hi = _span(r["old"])
            new_lo, new_hi = _span(r["new"])
        except (KeyError, ValueError):
            continue
        old_text = _line_range(head_lines(repo, r["path"]), old_lo, old_hi)
        wt_path = repo / r["path"]
        if not wt_path.is_file():
            continue
        new_text = _line_range(wt_path.read_text(encoding="utf-8", errors="replace").split("\n"), new_lo, new_hi)
        ratio = round(SequenceMatcher(None, old_text, new_text).ratio(), 2)
        cite = {"path": r["path"], "old": r["old"], "new": r["new"], "ratio": ratio,
                "old_text": old_text[:300], "new_text": new_text[:300], "source": "remap_rewritten"}
        # every "replaced" entry overlaps a changed hunk and belongs in the
        # batch; the ratio only picks how urgent the flag reads as.
        if ratio < SUBSTANTIVE_THRESHOLD:
            items.add(r["id"], "substantive", cite)
        elif ratio < LOW_SIM_THRESHOLD:
            items.add(r["id"], "low_sim", cite)
        else:
            items.add(r["id"], "replaced", cite)


QUOTE_WORDS = 5


def _norm_words(text: str) -> list[str]:
    return re.sub(r"[^a-z0-9_]+", " ", text.lower()).split()


def _removed_grams(removed: dict) -> list[tuple[str, str, str]]:
    """(path, original line, n-gram) for every QUOTE_WORDS-word window of a deleted line."""
    out = []
    for path, lines in removed.items():
        for line in lines:
            words = _norm_words(line)
            for i in range(len(words) - QUOTE_WORDS + 1):
                out.append((path, line.strip(), " ".join(words[i:i + QUOTE_WORDS])))
    return out


def from_rescan(items: ItemSet, R, E: dict, P: dict, mappers: dict, modified: set, have_pre: bool,
                removed: dict | None = None) -> None:
    grams = _removed_grams(removed or {})
    for item_id, txt in E.items():
        paths_here = {p for p in R.PATH_RE.findall(txt) if p in modified}
        risky = len(paths_here) >= 2 and (R.DETACHED_COLON_RE.search(txt) or R.PROSE_RE.search(txt))
        if risky:
            items.add(item_id, "assoc_risk", {"paths": sorted(paths_here), "source": "rescan_assoc_risk"})

        if grams:
            joined = " " + " ".join(_norm_words(" ".join(_cite_values(txt, R)))) + " "
            seen = set()
            for path, line, gram in grams:
                if (path, line) in seen or " " + gram + " " not in joined:
                    continue
                seen.add((path, line))
                items.add(item_id, "quoted_removed", {"path": path, "text": line, "source": "rescan_quoted_removed"})

        if not have_pre:
            continue
        pre_block = P.get(item_id, "")
        for val in _cite_values(txt, R):
            for m in R.PROSE_RE.finditer(val):
                path = _nearest_path_any(val, m.start(), R)
                if path is None or path not in mappers:
                    continue
                ctx = val[max(0, m.start() - 25):m.end()]
                if ctx not in pre_block:
                    continue
                nums = [int(x) for x in re.findall(r"\d+", m.group(2))]
                mapped = [mappers[path](n) for n in nums]
                if all(kind == "same" for _new, kind in mapped):
                    continue
                items.add(item_id, "prose_far", {
                    "path": path, "text": m.group(0),
                    "mapped": [(n, nv, kind) for n, (nv, kind) in zip(nums, mapped)],
                    "source": "rescan_prose",
                })


def _cite_values(block: str, R) -> list[str]:
    """The note/description/why/reason/evidence values of one block, first line each,
    the same lines remap_line_refs.process_status_file rewrites."""
    out = []
    for line in block.split("\n"):
        km = R.KEY_RE.match(line)
        if km:
            out.append(line[km.end():])
    return out


def _nearest_path_any(text: str, before: int, R):
    """Last PATH_RE match ending at or before `before`, modified or not."""
    found = None
    for m in R.PATH_RE.finditer(text, 0, before):
        found = m.group(1)
    return found


def _hints(it: dict) -> list[str]:
    """Short free-text pointers for the reviewer, one per relevant flag or cite."""
    hints = []
    flags = it["flags"]
    if "deleted" in flags:
        hints.append("cited line no longer exists in the working tree")
    if "unparsed" in flags:
        hints.append("remap could not parse this citation with confidence; check the line reference by hand")
    if "substantive" in flags:
        hints.append("cited line changed substantially; the note or description likely no longer matches it")
    elif "low_sim" in flags:
        hints.append("cited line was rewritten; check whether the note or description still matches it")
    elif "replaced" in flags:
        hints.append("cited line falls inside an edited hunk; confirm it still supports the note")
    for c in it["cites"]:
        if c.get("source") in ("remap_assoc_risk", "rescan_assoc_risk"):
            ps = c.get("paths") or []
            hints.append(f"names {len(ps)} modified files {ps}; confirm the citation points into the intended one")
        elif c.get("source") == "rescan_prose":
            hints.append(f"prose citation naming {c.get('path')} moved; confirm the number still points at the described content")
        elif c.get("source") == "rescan_quoted_removed":
            hints.append(f"quotes a line the edit pass removed from {c.get('path')} ({c.get('text')!r}); read the replacement and reword the quote")
    return hints


def build(run, status_path: Path, report_path: Path | None, pre_path: Path | None,
          models, pass_n: int, unbatched_ids: list[str] | None) -> dict:
    R = _load_remap_module(run.skill)

    status_text = status_path.read_text(encoding="utf-8")
    E = blocks(status_text)
    P = blocks(pre_path.read_text(encoding="utf-8")) if pre_path else {}

    hunks_by_path = hunks(run.repo)
    mappers = {p: R.build_mapper(h) for p, h in hunks_by_path.items()}
    modified = set(diff_names(run.repo))

    items = ItemSet()

    report = None
    if report_path and report_path.is_file():
        report = json.loads(report_path.read_text(encoding="utf-8"))
        from_report(items, report, run.repo)

    from_rescan(items, R, E, P, mappers, modified, have_pre=bool(pre_path), removed=removed_lines(run.repo))

    if pass_n == 1:
        selected = list(items.items.keys())
    else:
        selected = list(unbatched_ids or [])
        for item_id in selected:
            items.items.setdefault(item_id, {"flags": set(), "cites": []})

    out = []
    for item_id in selected:
        if item_id not in E:
            continue
        it = items.items[item_id]
        ref = get_field(status_text, item_id, "ref")
        kind = "debt" if ref else "claim"
        out.append({
            "id": item_id,
            "kind": kind,
            "status": get_field(status_text, item_id, "status") if kind == "claim" else None,
            "ref": ref if kind == "debt" else None,
            "ref_status": get_field(status_text, ref, "status") if kind == "debt" and ref in E else None,
            "flags": sorted(it["flags"]),
            "cites": it["cites"],
            "hints": _hints(it),
            "text": E[item_id],
            "pre_text": P.get(item_id, ""),
        })

    recon_dir = paths.stage_dir(run, "recon")
    items_path = recon_dir / ("recon-items.json" if pass_n == 1 else "pass2/recon2-items.json")
    if report is None and items_path.is_file():
        try:
            prev = json.loads(items_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            prev = None
        if prev and len(out) < len(prev):
            raise RuntimeError(
                f"no --report given (or none found) and this rescan selected only "
                f"{len(out)} item(s), fewer than the {len(prev)} left by a previous "
                f"run at {items_path}; refusing to overwrite it"
            )

    batch_dir = recon_dir if pass_n == 1 else recon_dir / "pass2"
    batch_dir.mkdir(parents=True, exist_ok=True)

    heavy = [o for o in out if set(o["flags"]) - {"assoc_risk"}]
    light = [o for o in out if not (set(o["flags"]) - {"assoc_risk"})]
    batch_paths = []
    n = 0
    for group, size in ((heavy, HEAVY_BATCH_SIZE), (light, LIGHT_BATCH_SIZE)):
        for k in range(0, len(group), size):
            n += 1
            p = batch_dir / f"batch-{n:02d}.json"
            p.write_text(json.dumps({"batch": n, "items": group[k:k + size]}, indent=1, ensure_ascii=False), encoding="utf-8")
            batch_paths.append(str(p))

    items_path.write_text(json.dumps(out, indent=1, ensure_ascii=False), encoding="utf-8")

    args_out = {
        "repo": str(run.repo),
        "python": str(run.python),
        "date": run.date,
        "batches": batch_paths,
    }
    if models is not None:
        args_out["models"] = models
    args_path = recon_dir / ("recon-args.json" if pass_n == 1 else "pass2/recon2-args.json")
    args_path.write_text(json.dumps(args_out, indent=1, ensure_ascii=False), encoding="utf-8")

    return {
        "total": len(out), "heavy": len(heavy), "light": len(light),
        "batches": len(batch_paths), "items_path": str(items_path), "args_path": str(args_path),
        "flag_counts": {f: sum(1 for o in out if f in o["flags"]) for f in
                         ("low_sim", "deleted", "substantive", "assoc_risk", "prose_far", "replaced", "unparsed", "quoted_removed")},
        "have_report": report is not None, "have_pre": pre_path is not None,
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run", required=True, help="path to run.json")
    parser.add_argument("--status", help="path to STATUS.yaml (default: run.repo/STATUS.yaml)")
    parser.add_argument("--pre", help="path to a STATUS.yaml snapshot from before any remap ran")
    parser.add_argument("--report", help="path to remap_line_refs.py's JSON --report output "
                                          "(default: run.scratch/i5/remap-report.json)")
    parser.add_argument("--pass", dest="pass_n", type=int, choices=(1, 2), default=1)
    parser.add_argument("--unbatched", help="JSON file: a list of ids to rescan (required with --pass 2)")
    parser.add_argument("--models", help="path to a JSON file of per-role model overrides")
    args = parser.parse_args(argv)

    if args.pass_n == 2 and not args.unbatched:
        print("error: --pass 2 requires --unbatched FILE", file=sys.stderr)
        return 1

    run = load_run(args.run)

    status_path = Path(args.status) if args.status else run.repo / "STATUS.yaml"
    if not status_path.is_file():
        print(f"error: STATUS.yaml not found at {status_path}", file=sys.stderr)
        return 1

    report_path = Path(args.report) if args.report else paths.stage_dir(run, "i5") / "remap-report.json"
    pre_path = Path(args.pre) if args.pre else None
    if pre_path and not pre_path.is_file():
        print(f"error: --pre file not found: {pre_path}", file=sys.stderr)
        return 1
    if not pre_path:
        print("note: no --pre given, prose citations are not rescanned (assoc_risk and the "
              "remap report's own flags still apply)", file=sys.stderr)
    if not report_path.is_file():
        print(f"note: no remap report at {report_path}, deleted/low_sim/substantive from it are skipped", file=sys.stderr)

    unbatched_ids = None
    if args.unbatched:
        unbatched_ids = json.loads(Path(args.unbatched).read_text(encoding="utf-8"))

    models = None
    if args.models:
        models = json.loads(Path(args.models).read_text(encoding="utf-8"))

    try:
        result = build(run, status_path, report_path, pre_path, models, args.pass_n, unbatched_ids)
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    print(f"pass={args.pass_n} items={result['total']} (heavy {result['heavy']}, light {result['light']}) "
          f"batches={result['batches']}")
    print(f"flags: {result['flag_counts']}")
    print(f"wrote {result['items_path']}")
    print(f"wrote {result['args_path']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
