#!/usr/bin/env python3
"""Build the args file for the I.6 mapping-audit workflow.

Samples N checkable statements out of the pre-edit text of this repo's
markdown docs (git show HEAD:<doc>, so the sample reflects what a doc
fixer started from, not what it produced) and writes them to
run.research/i6/sample.json for auditor A's fixed sample. Also writes
run.scratch/i6/i6-args.json, the {repo, python, sample_path, out_dir,
docs, models?} object the ground-truth-i6 workflow expects as `args`.

Doc list defaults to every tracked *.md, *.mdx, *.rst and *.adoc file
except docs/_human/**, the same convention used for the I.5 doc-fix stage. Sampling is a plain
paragraph/bullet heuristic (see _paragraphs/_is_candidate below), not a
model call -- it only needs to produce a plausible, reproducible sample
for auditor A to check; auditor B independently self-samples the same
population inside the workflow.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import random
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from gt_lib.git import ls_files, show  # noqa: E402
from gt_lib.paths import args_path, load_run  # noqa: E402

_DOC_GLOBS = ("*.md", "*.mdx", "*.rst", "*.adoc")
_EXCLUDE_GLOBS = ("docs/_human/**",)
_LIST_MARK_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
_MIN_LEN, _MAX_LEN = 30, 600


def default_docs(run) -> list[str]:
    """Every tracked doc file (*.md, *.mdx, *.rst, *.adoc), minus docs/_human/**."""
    all_md = ls_files(run.repo, *_DOC_GLOBS)
    return [p for p in all_md if not any(fnmatch.fnmatch(p, g) for g in _EXCLUDE_GLOBS)]


def _paragraphs(text: str):
    """Yield (start_line, block_text) for each blank-line/list-item/fence-delimited block."""
    lines = text.split("\n")
    block: list[str] = []
    start = None
    in_fence = False
    for i, line in enumerate(lines, start=1):
        stripped = line.strip()
        if stripped.startswith("```"):
            if block:
                yield start, "\n".join(block)
                block = []
            in_fence = not in_fence
            start = None
            continue
        if in_fence:
            continue
        is_new_item = bool(_LIST_MARK_RE.match(line)) and block
        if not stripped or is_new_item:
            if block:
                yield start, "\n".join(block)
                block = []
            if stripped:
                block = [line]
                start = i
            else:
                start = None
            continue
        if not block:
            start = i
        block.append(line)
    if block:
        yield start, "\n".join(block)


def _is_candidate(block: str) -> bool:
    """A checkable-looking statement: substantial prose, not a heading/fence/table/quote."""
    first = block.lstrip()
    if not first or first[0] in "#|>":
        return False
    text = re.sub(r"\s+", " ", block).strip()
    if not (_MIN_LEN <= len(text) <= _MAX_LEN):
        return False
    return bool(re.search(r"[A-Za-z]{3,}", text))


_CODE_SPAN_RE = re.compile(r"`([^`]+)`")


def _code_area_hint(text: str) -> str:
    """First backtick span in the statement, if any -- a hint at what code it names."""
    m = _CODE_SPAN_RE.search(text)
    return m.group(1) if m else ""


def collect_candidates(run, docs: list[str]) -> list[dict]:
    """Every candidate statement across docs, at HEAD (pre-edit)."""
    out = []
    for doc in docs:
        text = show(run.repo, "HEAD", doc)
        if text is None:
            continue
        for line, block in _paragraphs(text):
            if not _is_candidate(block):
                continue
            quote = re.sub(r"\s+", " ", block).strip()
            out.append({
                "old_location": f"{doc}:{line}",
                "old_claim_quote": quote,
                "normative_claim_text": quote,
                "code_area_hint": _code_area_hint(quote),
            })
    return out


def build_sample(run, docs: list[str], n: int, seed: int) -> tuple[list[dict], int]:
    candidates = collect_candidates(run, docs)
    rng = random.Random(seed)
    if len(candidates) <= n:
        sample = list(candidates)
    else:
        sample = rng.sample(candidates, n)
    sample.sort(key=lambda c: c["old_location"])
    return sample, len(candidates)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run", required=True, help="path to run.json")
    parser.add_argument("--n", type=int, default=18, help="sample size for auditor A (default 18)")
    parser.add_argument("--seed", type=int, default=0, help="RNG seed for reproducible sampling")
    parser.add_argument("--models", help="path to a JSON file with role model overrides")
    args = parser.parse_args(argv)

    run = load_run(args.run)
    docs = default_docs(run)
    if not docs:
        print("no tracked *.md/*.mdx/*.rst/*.adoc docs found (outside docs/_human/**)", file=sys.stderr)
        return 1

    sample, n_candidates = build_sample(run, docs, args.n, args.seed)

    research_dir = run.research / "i6"
    research_dir.mkdir(parents=True, exist_ok=True)
    sample_path = research_dir / "sample.json"
    sample_path.write_text(json.dumps(sample, indent=1, ensure_ascii=False), encoding="utf-8")

    payload = {
        "repo": str(run.repo),
        "python": str(run.python),
        "sample_path": str(sample_path),
        "out_dir": str(research_dir),
        "docs": docs,
    }
    if args.models:
        payload["models"] = json.loads(Path(args.models).read_text(encoding="utf-8"))

    out_path = args_path(run, "i6")
    out_path.write_text(json.dumps(payload, indent=1, ensure_ascii=False), encoding="utf-8")

    print(f"docs={len(docs)} candidates={n_candidates} sampled={len(sample)}/{args.n}")
    print(f"wrote {sample_path}")
    print(f"wrote {out_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
