#!/usr/bin/env python3
"""Validate the four I.1 research JSON files against pipeline/schemas.

Reads inventory.json, claims.json, every codemap-*.json and
debt-candidates.json from run.research, checks required keys and value
types against the matching pipeline/schemas/*.schema.json fragment with
a small stdlib validator (no jsonschema dependency), and prints a count
table plus every violation found.

With --journal (or --session-dir + --workflow-id) the stage first
materializes those files from the finders' structured return values in
the workflow journal, overwriting whatever the agents wrote by hand. The
return value is what the workflow schema validated; the file an agent
writes itself is not (on the fixture run one finder skipped its file and
another wrote a different top-level key), so the journal is the source.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from gt_lib.journal import journal_path, read_journal, task_output  # noqa: E402
from gt_lib.paths import load_run  # noqa: E402

SCHEMAS_DIR = Path(__file__).resolve().parent.parent / "schemas"

_TYPE_MAP = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "array": list,
    "object": dict,
}


def _check_type(value, type_name: str, where: str) -> list[str]:
    py_type = _TYPE_MAP.get(type_name)
    if py_type is None:
        return []
    if type_name == "integer" and isinstance(value, bool):
        return [f"{where}: expected integer, got boolean"]
    if not isinstance(value, py_type):
        return [f"{where}: expected {type_name}, got {type(value).__name__}"]
    return []


def validate(data, schema: dict, where: str = "$", strict: bool = False) -> list[str]:
    """Return a list of violation strings for data against schema (required keys + types only)."""
    errors: list[str] = []
    schema_type = schema.get("type")
    if schema_type:
        errors += _check_type(data, schema_type, where)
        if errors:
            return errors

    if schema_type == "object" and isinstance(data, dict):
        props = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in data:
                errors.append(f"{where}: missing required key {key!r}")
        for key, value in data.items():
            if key in props:
                # unknown-key enforcement is top-level only: never recurse strict
                errors += validate(value, props[key], f"{where}.{key}", False)
            elif strict:
                errors.append(f"{where}: unknown key {key!r}")

    elif schema_type == "array" and isinstance(data, list):
        item_schema = schema.get("items")
        if item_schema:
            for i, item in enumerate(data):
                errors += validate(item, item_schema, f"{where}[{i}]", False)

    return errors


def _load(path: Path):
    if not path.exists():
        return None, [f"missing file: {path}"]
    try:
        return json.loads(path.read_text(encoding="utf-8")), []
    except json.JSONDecodeError as exc:
        return None, [f"{path}: invalid JSON ({exc})"]


def _print_count_row(label: str, count) -> None:
    print(f"  {label}: {count}")


def _target_name(result: dict) -> str | None:
    """Pick the research file a finder result belongs to, by shape first, then by its written_to basename."""
    if "files" in result and "onboarding_chain" in result:
        return "inventory.json"
    if "claims_found" in result:
        return "claims.json"
    if "suspects" in result:
        return "debt-candidates.json"
    if "components" in result:
        base = Path(str(result.get("written_to", ""))).name
        if base.startswith("codemap-") and base.endswith(".json"):
            return base
        roots = {c.get("root") for c in result["components"] if isinstance(c, dict) and c.get("root")}
        if len(roots) == 1:
            return f"codemap-{roots.pop()}.json"
    return None


def materialize(research: Path, entries: list[dict]) -> list[str]:
    """Write each finder result from the journal into research/, return the file names written."""
    written: list[str] = []
    for e in entries:
        r = e.get("result")
        if not isinstance(r, dict):
            continue
        name = _target_name(r)
        if name is None:
            continue
        target = research / name
        payload = dict(r)
        payload["written_to"] = str(target)
        research.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        written.append(name)
    return written


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Validate I.1 research JSON files against pipeline/schemas.")
    parser.add_argument("--run", required=True, help="path to run.json")
    parser.add_argument("--strict", action="store_true", help="also fail on unknown top-level keys")
    parser.add_argument("--journal", help="path to the i1-research.js workflow's journal.jsonl; research files are rewritten from it")
    parser.add_argument("--session-dir", help="Claude session directory (used with --workflow-id)")
    parser.add_argument("--workflow-id", help="workflow id under session-dir/subagents/workflows/")
    args = parser.parse_args(argv)

    run = load_run(args.run)

    journal = args.journal
    if not journal and args.session_dir and args.workflow_id:
        journal = journal_path(args.session_dir, args.workflow_id)
    if journal:
        jp = Path(journal)
        if not jp.exists():
            print(f"journal not found: {jp}")
            return 1
        names = materialize(run.research, read_journal(jp))
        print(f"materialized {len(names)} file(s) from journal: {', '.join(sorted(names)) or '-'}")

    schema_inventory = json.loads((SCHEMAS_DIR / "inventory.schema.json").read_text(encoding="utf-8"))
    schema_claims = json.loads((SCHEMAS_DIR / "claims.schema.json").read_text(encoding="utf-8"))
    schema_codemap = json.loads((SCHEMAS_DIR / "codemap.schema.json").read_text(encoding="utf-8"))
    schema_debt = json.loads((SCHEMAS_DIR / "debt-candidates.schema.json").read_text(encoding="utf-8"))

    total_errors: list[str] = []

    print("I.1 research check")

    inv_path = run.research / "inventory.json"
    inv, errs = _load(inv_path)
    total_errors += errs
    if inv is not None:
        total_errors += validate(inv, schema_inventory, str(inv_path), args.strict)
        _print_count_row("inventory files", len(inv.get("files", [])))

    claims_path = run.research / "claims.json"
    claims, errs = _load(claims_path)
    total_errors += errs
    if claims is not None:
        total_errors += validate(claims, schema_claims, str(claims_path), args.strict)
        _print_count_row("claims", len(claims.get("claims_found", [])))

    codemap_paths = sorted(run.research.glob("codemap-*.json"))
    if not codemap_paths:
        total_errors.append(f"missing file: {run.research}/codemap-*.json (no codemap files found)")
    total_components = 0
    for cm_path in codemap_paths:
        cm, errs = _load(cm_path)
        total_errors += errs
        if cm is not None:
            total_errors += validate(cm, schema_codemap, str(cm_path), args.strict)
            n = len(cm.get("components", []))
            total_components += n
            _print_count_row(f"components ({cm_path.name})", n)

    debt_path = run.research / "debt-candidates.json"
    debt, errs = _load(debt_path)
    total_errors += errs
    if debt is not None:
        total_errors += validate(debt, schema_debt, str(debt_path), args.strict)
        _print_count_row("debt suspects", len(debt.get("suspects", [])))

    if total_errors:
        print(f"\n{len(total_errors)} violation(s):")
        for e in total_errors:
            print(f"  - {e}")
        return 1

    print(f"\nOK: {total_components} component(s) across {len(codemap_paths)} codemap file(s), no violations")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
