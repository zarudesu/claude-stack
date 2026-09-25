#!/usr/bin/env python3
"""Remap file:line citations in STATUS.yaml from a git ref to the working tree.

STATUS.yaml cites code as file:line inside its note:/description:/why:/
reason:/evidence: values, written against a base commit (default HEAD).
Comment-only or other line-shifting edits since that base leave the
citations pointing at the wrong line. This reads
`git diff -U0 --diff-filter=M <base> -- .`, builds an old-line -> new-line
map per changed file, and rewrites citations that fall in a changed file.

Four citation shapes are handled:
  - path:N, path:N-M, and comma/"and" lists of either, right after a path
  - "path.py lines N and M" / "path.py (lines N-M)", right after a path
  - a detached :N or :N-M with no path directly before it -- the nearest
    earlier path mention in the same value is used
  - prose "line N" / "lines N-M and P-Q" with no path directly before it,
    same nearest-path rule, plus a stronger safety check (see below)

A detached or prose citation is only ever resolved against a path if the
value names exactly one changed path; a value naming two or more changed
paths together with a detached or prose citation is ambiguous about which
path it means, so it is reported (assoc_risk in the JSON report) and left
untouched rather than guessed at.

A prose citation is additionally rewritten only when its own text (a
short window ending at the citation) is byte-identical in --pre, a copy
of STATUS_FILE saved before any remapping against this base ran. Skipping
this check would let a second run re-associate an already-rewritten
number with a different path, or shift it a second time; --pre is
optional on the command line but the rewrite is skipped without it.

The mapping is <base> numbering -> working-tree numbering. A citation is
only remapped once against a given base: if STATUS_FILE's own line
carrying the citation already differs from that base (--apply rewrote
it, or the session wrote it fresh looking at the working tree), that
line is treated as already in working-tree coordinates and left alone
by both --check and --apply, so a second run against the same base does
not shift it again. A STATUS_FILE with no counterpart at <base> is
treated as entirely working-tree coordinates.

Usage: remap_line_refs.py [--base HEAD] [--apply] [--dry-run] [--check]
                           [--pre PATH] [--report PATH] STATUS_FILE

May be run from any directory inside the repository -- the diff and the
changed-file check are both resolved against the repository root, not
the current working directory.

Dry run by default (also explicit with --dry-run): prints the counts
line and touches nothing; --report PATH additionally writes a JSON
report with counts and four lists (rewritten, deleted, unparsed,
assoc_risk). --apply validates the rewritten text with
yaml.safe_load before touching STATUS_FILE, then replaces it in place
atomically; a result that fails to parse is reported and STATUS_FILE is
left untouched. --dry-run always wins if both are given.

--check never writes anything, not even the JSON report: it prints one
line per stale reference and a fix: hint, then exits 1 if it found any
(0 if the file is clean, 2 on error) -- the shape a Stop hook wants.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys

import yaml

KEY_RE = re.compile(r"^\s*(?:- )?(note|description|why|reason|evidence):\s")
ID_RE = re.compile(r"^\s*- id:\s*(\S+)")
HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))?\s\+(\d+)(?:,(\d+))?\s@@")
PLUS_PATH_RE = re.compile(r"^\+\+\+ b/(.+)$")
EXT = r"(?:py|yml|yaml|md|j2|cfg|ini|toml|sh|go|js|ts|html|txt|sql|json|example|conf|service)"
PATH_RE = re.compile(r"(?<![\w/.-])((?:[\w.-]+/)*[\w.-]+\." + EXT + r")")
NUM_RE = re.compile(r"(\d+)(?:\s*(?:-|–|\sto\s)\s*(\d+))?")
SEP_COLON_RE = re.compile(r"(?:,\s*and\s+|,\s+|\s+and\s+):")
SEP_PLAIN_RE = re.compile(r"(?:,\s*and\s+|,\s+|\s+and\s+)")
LINE_PREFIXES = [
    (re.compile(r"\s+at\s+lines?\s+"), False),
    (re.compile(r"\s+\(lines?\s+"), True),
    (re.compile(r"\s+lines?\s+"), False),
    (re.compile(r",\s+lines?\s+"), False),
]

# A ':N' or ':N-M' with no path-shaped character right before the colon --
# the path it belongs to (if any) is the nearest PATH_RE match earlier in
# the same value, found by _nearest_path.
DETACHED_COLON_RE = re.compile(r"(?<![\w/.\-\]]):(\d+)(?:-(\d+))?(?![\w-])")

# Prose "line N" / "lines N, M and P-Q", same nearest-path rule as above.
PROSE_RE = re.compile(
    r"\b(lines?)\s+(\d+(?:-\d+)?(?:(?:,\s*(?:and\s+)?|\s+and\s+)\d+(?:-\d+)?)*)"
)

# A minimal id-block splitter, kept self-contained here (this file is
# copied into a target repository on its own, with no library import
# available) rather than shared with the skill's own yaml_edit module.
_ENTRY_RE = re.compile(r"^( *)- id: (\S+)$\n((?:\1  .*\n)+)", re.M)


def _pre_blocks(text: str) -> dict:
    return {m.group(2): m.group(0) for m in _ENTRY_RE.finditer(text)}


def _repo_root() -> str:
    return subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def parse_diff(base: str) -> dict[str, list[tuple[int, int, int, int]]]:
    """path -> list of (old_start, old_count, new_start, new_count) hunks.

    git diff reports paths relative to the repository root, so both the
    diff itself and the changed-file existence check below are run
    against that root -- not the process's cwd -- so this gives the same
    result no matter which directory inside the repository it runs from.
    """
    root = _repo_root()
    out = subprocess.run(
        ["git", "diff", "-U0", "--diff-filter=M", base, "--", "."],
        capture_output=True, text=True, check=True, cwd=root,
    ).stdout
    files: dict[str, list[tuple[int, int, int, int]]] = {}
    current = None
    for line in out.splitlines():
        if line.startswith("diff --git a/"):
            current = None
        elif (m := PLUS_PATH_RE.match(line)):
            current = m.group(1) if m.group(1) != "/dev/null" else None
            if current is not None:
                files.setdefault(current, [])
        elif current is not None and (m := HUNK_RE.match(line)):
            a, b, c, d = m.groups()
            files[current].append((int(a), int(b) if b else 1, int(c), int(d) if d else 1))
    return {p: h for p, h in files.items() if os.path.isfile(os.path.join(root, p))}


def build_mapper(hunks: list[tuple[int, int, int, int]]):
    """Return old_line -> (new_line_or_None, kind) for one file's hunks."""
    hunks = sorted(hunks, key=lambda h: h[0])

    def mapper(line: int):
        delta = 0
        for a, b, c, d in hunks:
            if b == 0:  # pure insertion of d lines after old line a
                if line <= a:
                    continue
                delta += d
            elif d == 0:  # pure deletion of old lines a..a+b-1
                if a <= line <= a + b - 1:
                    return None, "deleted"
                if line > a + b - 1:
                    delta -= b
            else:  # replacement of old a..a+b-1 with new c..c+d-1
                if a <= line <= a + b - 1:
                    return c + min(line - a, d - 1), "replaced"
                if line > a + b - 1:
                    delta += d - b
        return line + delta, ("same" if delta == 0 else "shifted")
    return mapper


def worktree_citation_lines(base: str, status_file: str) -> set[int] | None:
    """STATUS_FILE line numbers (working-tree side) that already differ
    from `base`, or None if every line counts as working-tree
    coordinates (no counterpart at `base`, or the diff for it errors).

    A citation sitting on one of these lines was written or rewritten
    looking at the working tree already, so mapping it again against
    the same base is exactly the double-shift this guards against.
    """
    root = _repo_root()
    rel = os.path.relpath(os.path.abspath(status_file), root)
    probe = subprocess.run(
        ["git", "cat-file", "-e", f"{base}:{rel}"], cwd=root, capture_output=True,
    )
    if probe.returncode != 0:
        return None
    try:
        out = subprocess.run(
            ["git", "diff", "-U0", base, "--", rel],
            capture_output=True, text=True, check=True, cwd=root,
        ).stdout
    except subprocess.CalledProcessError:
        return None
    lines: set[int] = set()
    for line in out.splitlines():
        m = HUNK_RE.match(line)
        if not m:
            continue
        _, _, c, d = m.groups()
        c = int(c)
        d = int(d) if d else 1
        lines.update(range(c, c + d))
    return lines


def match_number(text: str, pos: int):
    """(end, is_valid_range) for a number/range at pos, or None if no match."""
    m = NUM_RE.match(text, pos)
    if not m:
        return None
    valid = m.group(2) is None or int(m.group(2)) >= int(m.group(1))
    return m.end(), valid


def consume_list(text: str, end: int, sep_re: re.Pattern):
    """Extend end past ", :N" / " and N" style continuation items."""
    while True:
        m = sep_re.match(text, end)
        if not m:
            return end, False
        r = match_number(text, m.end())
        if r is None:
            return end, False
        end2, valid = r
        if not valid:
            return end2, True
        end = end2


def parse_colon_list(text: str, pos: int):
    if pos + 1 >= len(text) or text[pos] != ":" or not text[pos + 1].isdigit():
        return None
    r = match_number(text, pos + 1)
    if r is None:
        return None
    end, valid = r
    if not valid:
        return "invalid", end
    end, invalid = consume_list(text, end, SEP_COLON_RE)
    return ("invalid", end) if invalid else ("ok", end)


def parse_line_list(text: str, pos: int):
    for prefix_re, needs_paren in LINE_PREFIXES:
        m = prefix_re.match(text, pos)
        if not m:
            continue
        r = match_number(text, m.end())
        if r is None:
            continue
        end, valid = r
        if not valid:
            return "invalid", end
        end, invalid = consume_list(text, end, SEP_PLAIN_RE)
        if invalid:
            return "invalid", end
        if needs_paren:
            if end < len(text) and text[end] == ")":
                end += 1
            else:
                continue
        return "ok", end
    return None


def _nearest_path(text: str, before: int, mappers: dict):
    """Return the last PATH_RE match ending at or before `before`, or None.

    Every path counts, not just the changed ones. A detached ":N" written
    right after an unchanged path belongs to that path; skipping unchanged
    paths here would walk back past it to some earlier changed path and
    shift a citation that names a file this diff never touched. Callers
    drop the citation when the path that comes back is not in mappers.
    """
    found = None
    for m in PATH_RE.finditer(text, 0, before):
        found = m.group(1)
    return found


def _classify_span(span: str, nums: list[tuple[int, int, int]], mappers: dict, path: str):
    """Map every number in nums (start,end,value) through mappers[path]; return (new_span, kind)."""
    results = [mappers[path](n) for (_, _, n) in nums]
    if any(k == "deleted" for _, k in results):
        return None, "deleted"
    if any(k == "replaced" for _, k in results):
        kind = "replaced"
    elif any(nv != n for (nv, _), (_, _, n) in zip(results, nums)):
        kind = "shifted"
    else:
        return span, "same"
    new_span = span
    for (s, e, _n), (nv, _k) in reversed(list(zip(nums, results))):
        new_span = new_span[:s] + str(nv) + new_span[e:]
    return new_span, kind


def process_value(text, mappers, current_id, totals, rewritten, deleted, unparsed, assoc_risk, pre_blocks, line_no):
    """Rewrite every changed-file citation in one note/description value."""
    edits: list[tuple[int, int, str]] = []
    consumed: list[tuple[int, int]] = []
    changed_here = {m.group(1) for m in PATH_RE.finditer(text) if m.group(1) in mappers}
    risky = len(changed_here) >= 2

    for m in PATH_RE.finditer(text):
        path = m.group(1)
        if path not in mappers:
            continue
        path_end = m.end()
        res = parse_colon_list(text, path_end) or parse_line_list(text, path_end)
        if res is None:
            if re.search(r"\d", text[path_end:path_end + 15]):
                unparsed.append({"id": current_id, "path": path, "text": text[m.start():path_end + 15]})
                totals["unparsed"] += 1
            continue
        status, spec_end = res
        consumed.append((m.start(), spec_end))
        totals["found"] += 1
        totals["in_changed"] += 1
        if status == "invalid":
            unparsed.append({"id": current_id, "path": path, "text": text[m.start():spec_end]})
            totals["unparsed"] += 1
            continue
        span = text[path_end:spec_end]
        nums = [(nm.start(), nm.end(), int(nm.group())) for nm in re.finditer(r"\d+", span)]
        new_span, kind = _classify_span(span, nums, mappers, path)
        totals[kind] += 1
        if kind == "deleted":
            deleted.append({"id": current_id, "path": path, "text": span})
            continue
        if kind == "same":
            continue
        edits.append((path_end, spec_end, new_span))
        rewritten.append({"id": current_id, "path": path, "old": span, "new": new_span, "kind": kind, "line": line_no})

    def _overlaps(span):
        return any(cs <= span[0] < ce for cs, ce in consumed)

    for m in DETACHED_COLON_RE.finditer(text):
        if _overlaps(m.span()):
            continue
        path = _nearest_path(text, m.start(), mappers)
        if path is None or path not in mappers:
            continue
        span = m.group(0)
        nums = [(nm.start(), nm.end(), int(nm.group())) for nm in re.finditer(r"\d+", span)]
        if risky:
            assoc_risk.append({"id": current_id, "paths": sorted(changed_here), "text": span})
            continue
        totals["found"] += 1
        totals["in_changed"] += 1
        new_span, kind = _classify_span(span, nums, mappers, path)
        totals[kind] += 1
        if kind == "deleted":
            deleted.append({"id": current_id, "path": path, "text": span})
            continue
        if kind == "same":
            continue
        edits.append((m.start(), m.end(), new_span))
        rewritten.append({"id": current_id, "path": path, "old": span, "new": new_span, "kind": kind, "line": line_no})

    pre_block = pre_blocks.get(current_id) if pre_blocks is not None else None
    for m in PROSE_RE.finditer(text):
        if _overlaps(m.span()):
            continue
        path = _nearest_path(text, m.start(), mappers)
        if path is None or path not in mappers:
            continue
        span = m.group(0)
        if risky:
            assoc_risk.append({"id": current_id, "paths": sorted(changed_here), "text": span})
            continue
        if pre_blocks is None:
            unparsed.append({"id": current_id, "path": path, "text": span})
            totals["unparsed"] += 1
            continue
        ctx = text[max(0, m.start() - 25):m.end()]
        if pre_block is None or ctx not in pre_block:
            unparsed.append({"id": current_id, "path": path, "text": span})
            totals["unparsed"] += 1
            continue
        keyword, numlist = m.group(1), m.group(2)
        nums = [(nm.start(), nm.end(), int(nm.group())) for nm in re.finditer(r"\d+", numlist)]
        totals["found"] += 1
        totals["in_changed"] += 1
        new_numlist, kind = _classify_span(numlist, nums, mappers, path)
        totals[kind] += 1
        if kind == "deleted":
            deleted.append({"id": current_id, "path": path, "text": span})
            continue
        if kind == "same":
            continue
        new_span = keyword + " " + new_numlist
        edits.append((m.start(), m.end(), new_span))
        rewritten.append({"id": current_id, "path": path, "old": span, "new": new_span, "kind": kind, "line": line_no})

    for s, e, new in sorted(edits, key=lambda t: t[0], reverse=True):
        text = text[:s] + new + text[e:]
    return text


def process_status_file(lines: list[str], mappers: dict, pre_blocks=None, wt_lines=frozenset()):
    """wt_lines: STATUS_FILE line numbers (1-based) already in working-tree
    coordinates (see worktree_citation_lines) -- skipped rather than
    remapped again. None means every line counts as working-tree."""
    totals = dict.fromkeys(["found", "in_changed", "same", "shifted", "replaced", "deleted", "unparsed"], 0)
    rewritten, deleted, unparsed, assoc_risk = [], [], [], []
    current_id = "?"
    out_lines = list(lines)
    for idx, line in enumerate(lines):
        m = ID_RE.match(line)
        if m:
            current_id = m.group(1)
            continue
        km = KEY_RE.match(line)
        if not km:
            continue
        if wt_lines is None or (idx + 1) in wt_lines:
            continue
        value = line[km.end():]
        new_value = process_value(
            value, mappers, current_id, totals, rewritten, deleted, unparsed, assoc_risk, pre_blocks, idx + 1,
        )
        if new_value != value:
            out_lines[idx] = line[:km.end()] + new_value
    return out_lines, totals, rewritten, deleted, unparsed, assoc_risk


def write_report(path, totals, rewritten, deleted, unparsed, assoc_risk) -> None:
    report = {
        "totals": totals,
        "rewritten": rewritten,
        "deleted": deleted,
        "unparsed": unparsed,
        "assoc_risk": assoc_risk,
    }
    if path:
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps(report, indent=1, ensure_ascii=False))
    print(
        f"found={totals['found']} in_changed={totals['in_changed']} same={totals['same']} "
        f"shifted={totals['shifted']} replaced={totals['replaced']} deleted={len(deleted)} "
        f"unparsed={len(unparsed)} assoc_risk={len(assoc_risk)}"
    )


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base", default="HEAD")
    ap.add_argument("--apply", action="store_true", help="rewrite STATUS_FILE in place")
    ap.add_argument("--dry-run", action="store_true", help="never write STATUS_FILE, even with --apply")
    ap.add_argument("--check", action="store_true", help="report stale references only, no write, exit 1 if any")
    ap.add_argument("--pre", help="path to STATUS_FILE as it was before any remap against this base")
    ap.add_argument("--report", help="also write the JSON report to PATH (off by default)")
    ap.add_argument("status_file")
    args = ap.parse_args(argv)
    err_code = 2 if args.check else 1

    try:
        changed = parse_diff(args.base)
    except subprocess.CalledProcessError as exc:
        print(f"error: git diff failed: {exc}", file=sys.stderr)
        return err_code
    mappers = {p: build_mapper(h) for p, h in changed.items()}

    pre_blocks = None
    if args.pre:
        try:
            pre_blocks = _pre_blocks(open(args.pre, encoding="utf-8").read())
        except OSError as exc:
            print(f"error: cannot read --pre {args.pre}: {exc}", file=sys.stderr)
            return err_code
    elif not args.check:
        print("note: no --pre given, prose-form citations are reported but not rewritten", file=sys.stderr)

    try:
        with open(args.status_file, newline="", encoding="utf-8") as f:
            lines = f.read().splitlines(keepends=True)
    except OSError as exc:
        print(f"error: cannot read {args.status_file}: {exc}", file=sys.stderr)
        return err_code

    wt_lines = worktree_citation_lines(args.base, args.status_file)
    out_lines, totals, rewritten, deleted, unparsed, assoc_risk = process_status_file(
        lines, mappers, pre_blocks, wt_lines,
    )

    if args.check:
        for r in rewritten:
            old = r["old"][1:] if r["old"].startswith(":") else r["old"]
            new = r["new"][1:] if r["new"].startswith(":") else r["new"]
            print(f"{args.status_file}:{r['line']}: {r['path']}:{old} -> {new}")
        if rewritten:
            print(f"fix: {sys.executable} tools/ground_truth/remap_line_refs.py --apply {args.status_file}")
        return 1 if rewritten else 0

    write_report(args.report, totals, rewritten, deleted, unparsed, assoc_risk)

    if args.apply and not args.dry_run:
        new_text = "".join(out_lines)
        try:
            yaml.safe_load(new_text)
        except yaml.YAMLError as exc:
            print(f"error: refusing to write, result does not parse as YAML: {exc}", file=sys.stderr)
            return 1
        dirname = os.path.dirname(os.path.abspath(args.status_file)) or "."
        tmp_path = os.path.join(dirname, f".{os.path.basename(args.status_file)}.tmp{os.getpid()}")
        try:
            with open(tmp_path, "w", newline="", encoding="utf-8") as f:
                f.write(new_text)
            os.replace(tmp_path, args.status_file)
        except OSError as exc:
            print(f"error: failed to write {args.status_file}: {exc}", file=sys.stderr)
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(2 if "--check" in sys.argv[1:] else 1)
