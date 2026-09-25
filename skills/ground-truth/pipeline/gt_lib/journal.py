"""Read a workflow's journal.jsonl and its final <taskId>.output file.

Each journal.jsonl line is one agent lifecycle event: {"type": "started",
"key": ..., "agentId": ...} or {"type": "result", "key": ..., "agentId":
..., "result": {...}}. The harness does not persist the label/phase that
was passed to agent() back into the file, so `label` below is the raw
per-call key (opaque, but stable and unique per call) and `phase` is only
filled in when a result payload happens to carry its own "phase" field.
Every reader in this pipeline therefore discriminates results by shape
(which fields a result dict carries -- see results_where) rather than by
label; that is the fallback this module exists to make reusable (p.18).
"""
from __future__ import annotations

import json
from pathlib import Path


def journal_path(session_dir: str | Path, workflow_id: str) -> Path:
    """Compose <session_dir>/subagents/workflows/<workflow_id>/journal.jsonl."""
    return Path(session_dir) / "subagents" / "workflows" / str(workflow_id) / "journal.jsonl"


def read_journal(path: str | Path) -> list[dict]:
    """Read type=='result' lines from a journal.jsonl.

    Returns a list of {'label', 'phase', 'result'} dicts, one per agent
    call that produced a result. Lines that are not valid JSON, or whose
    type is not 'result', are skipped.
    """
    entries = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if d.get("type") != "result":
                continue
            result = d.get("result")
            phase = result.get("phase") if isinstance(result, dict) else None
            entries.append({"label": d.get("key"), "phase": phase, "result": result})
    return entries


def by_label(entries: list[dict]) -> dict:
    """Index journal entries by their label (last write wins on a duplicate)."""
    return {e["label"]: e for e in entries}


def results_where(entries: list[dict], key: str) -> list:
    """Return the result payloads that are dicts containing `key`, in journal order.

    Batch-shaped results (judge batches, recon-review batches, raise-vote
    batches) are told apart by which fields they carry, since the journal
    does not label entries by phase.
    """
    return [
        e["result"] for e in entries
        if isinstance(e.get("result"), dict) and key in e["result"]
    ]


def task_output(path: str | Path):
    """Parse a <taskId>.output file and return its 'result' value.

    Tolerates a text prefix before the JSON object. Returns None if the
    file is missing or empty.
    """
    p = Path(path)
    if not p.exists() or p.stat().st_size == 0:
        return None
    text = p.read_text(encoding="utf-8")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        if start < 0:
            raise
        data = json.loads(text[start:])
    return data.get("result") if isinstance(data, dict) else None
