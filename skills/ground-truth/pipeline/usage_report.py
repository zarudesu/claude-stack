#!/usr/bin/env python3
"""Read existing usage logs without model calls, invented pricing or double-counting."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

KEYS = ("input_uncached", "input_cache_read", "input_cache_write", "output")


def counts(usage, camel=False, codex=False):
    if camel:
        names = ("inputTokens", "cacheReadInputTokens", "cacheCreationInputTokens", "outputTokens")
    elif codex:
        names = ("input_tokens", "cached_input_tokens", None, "output_tokens")
    else:
        names = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens")
    result = {key: int(usage.get(name) or 0) if name else 0 for key, name in zip(KEYS, names)}
    if codex:
        result["input_uncached"] -= result["input_cache_read"]
    if any(v < 0 for v in result.values()):
        raise ValueError("invalid negative token counts")
    return result


def sum_counts(values):
    return {key: sum(v[key] for v in values) for key in KEYS}


def analyze(records):
    results, messages, codex, warnings = [], {}, None, []
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            continue
        if record.get("type") == "result":
            results.append(record)
        message = record.get("message", {})
        if record.get("type") == "assistant" and isinstance(message, dict) and message.get("usage"):
            identity = message.get("id")
            if not identity:
                identity = f"anonymous-{index}"
                warnings.append("assistant usage without message.id: cannot deduplicate it")
            # Partial stream records can repeat the same message; retain latest fields.
            messages[identity] = {**messages.get(identity, {}), **message["usage"]}
        payload = record.get("payload") or {}
        if isinstance(payload, dict) and payload.get("type") == "token_count":
            usage = (payload.get("info") or {}).get("total_token_usage")
            if usage:
                codex = usage
    cost, token_counts, source = None, None, "unknown"
    session_id = None
    if results:
        result = results[-1]
        session_id = result.get("session_id")
        if len(results) > 1:
            warnings.append("multiple results: using last only; inspect session/continuation boundaries")
        cost = result.get("total_cost_usd")
        if cost is not None and (not isinstance(cost, (int, float)) or cost < 0):
            raise ValueError("invalid total_cost_usd")
        models = result.get("modelUsage")
        if isinstance(models, dict) and models:
            token_counts = sum_counts([counts(v, camel=True) for v in models.values()])
            source = "claude.result.modelUsage"
        elif result.get("usage"):
            token_counts = counts(result["usage"])
            source = "claude.result.usage"
    if token_counts is None and codex is not None:
        token_counts, source = counts(codex, codex=True), "codex.cumulative"
        warnings.append("cumulative usage does not establish that the task completed")
    if token_counts is None and messages:
        token_counts = sum_counts([counts(v) for v in messages.values()])
        source = "claude.messages"
        warnings.append("no final usage summary: visible messages may omit child agents or retries")
    if token_counts is None:
        warnings.append("no supported token accounting found")
    return {"source": source, "session_id": session_id, "tokens": token_counts,
            "reported_cost_usd": cost, "warnings": list(dict.fromkeys(warnings))}


def read_report(path):
    invalid = 0
    # Stream line by line; never emit transcript text or prompts in the report.
    def records():
        nonlocal invalid
        with Path(path).open(encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    invalid += 1
    report = {"file": str(path), **analyze(records())}
    if invalid:
        report["warnings"].append(f"{invalid} invalid JSONL lines; accounting may be incomplete")
    return report


def aggregate(reports):
    known_costs = [r["reported_cost_usd"] for r in reports if r["reported_cost_usd"] is not None]
    unknown = sum(r["tokens"] is None for r in reports)
    return {
        "files": reports,
        "visible_tokens": sum_counts([r["tokens"] for r in reports if r["tokens"] is not None]),
        "unknown_token_files": unknown,
        "known_reported_cost_usd": sum(known_costs) if known_costs else None,
        "unknown_cost_files": len(reports) - len(known_costs),
        "complete_reported_cost_usd": sum(known_costs)
            if reports and len(known_costs) == len(reports) and not any(r["warnings"] for r in reports) else None,
        "caveat": "API-equivalent reported cost, not subscription billing; do not combine parent totals with child logs.",
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("transcripts", nargs="+", type=Path)
    args = parser.parse_args(argv)
    reports, paths, sessions = [], set(), set()
    try:
        for path in args.transcripts:
            if path.resolve() in paths:
                raise ValueError(f"duplicate file: {path}")
            paths.add(path.resolve())
            report = read_report(path)
            session = report.get("session_id")
            if session and session in sessions:
                raise ValueError(f"duplicate final session_id: {session}; choose one non-overlapping log")
            if session:
                sessions.add(session)
            reports.append(report)
    except (OSError, ValueError, TypeError) as exc:
        parser.exit(2, f"usage_report: {exc}\n")
    print(json.dumps(aggregate(reports), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
