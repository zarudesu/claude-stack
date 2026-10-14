# Status contract storage: where the contract lives and how to read it. Copied from the ground-truth skill templates; keep in sync with the copy there.
"""Two layouts hold the same contract:

  legacy   STATUS.yaml at the root
  sharded  .ground-truth/contract.yaml (schema_version, meta, edges),
           .ground-truth/areas/<id>.yaml (area, sources, claims),
           .ground-truth/debt.yaml (debt)

Only this module knows those file names. load() returns the legacy dict
shape for either layout, so the checks above it do not care which one is
on disk. Both STATUS.yaml and contract.yaml present is refused.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

# yaml is imported where a file is parsed: the edit guard asks only for
# file names and mtimes on every edit and must not pay for the parser.

LEGACY, SHARDED, NONE = "legacy", "sharded", "none"
STATUS = "STATUS.yaml"
DIR = ".ground-truth"
META = f"{DIR}/contract.yaml"
DEBT = f"{DIR}/debt.yaml"
AREAS = f"{DIR}/areas"
MODEL = f"{DIR}/model.yaml"
AREA_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
CLAIM_REF_RE = re.compile(r"(?:STATUS\.yaml#|\bclaim:)([A-Za-z0-9_]+)")
_LINES_REF_RE = re.compile(r"(?P<path>[^\s:]+):(?P<start>\d+)(?:-(?P<end>\d+))?(?:\s+«(?P<quote>[^»]*)»)?")
_KEYS = {META: {"schema_version", "meta", "edges"}, DEBT: {"debt"}}
_AREA_KEYS = {"area", "sources", "claims"}


class ContractError(Exception):
    """The contract is missing, does not parse, or both layouts are present."""


@dataclass
class Contract:
    data: dict
    # ("claim" | "debt", id) -> (file, first line, last line), first occurrence wins
    origin: dict = field(default_factory=dict)
    area_of_claim: dict = field(default_factory=dict)
    # (claim id or "-", message) for shard rules check_shape reports as FAIL
    problems: list = field(default_factory=list)


def find_root(start: Path) -> Path | None:
    for candidate in [start, *start.parents]:
        if (candidate / STATUS).is_file() or (candidate / META).is_file():
            return candidate
    return None


def _files(exists, areas: list[str]) -> list[str]:
    legacy, sharded = exists(STATUS), exists(META)
    if legacy and sharded:
        raise ContractError(
            f"both {STATUS} and {META} exist; run `git checkout` or finish `gt migrate`"
        )
    if legacy:
        return [STATUS]
    if not sharded:
        return []
    return [META] + ([DEBT] if exists(DEBT) else []) + sorted(areas)


def _is_area_file(rel: str) -> bool:
    return Path(rel).parent.as_posix() == AREAS and rel.endswith(".yaml")


def contract_files(root: Path) -> list[str]:
    areas = [p.relative_to(root).as_posix() for p in (root / AREAS).glob("*.yaml") if p.is_file()]
    return _files(lambda rel: (root / rel).is_file(), areas)


def detect_layout(root: Path) -> str:
    files = contract_files(root)
    return LEGACY if files == [STATUS] else SHARDED if files else NONE


def contract_name(root: Path) -> str:
    return "contract (.ground-truth/)" if detect_layout(root) == SHARDED else STATUS


def is_contract_file(rel: str) -> bool:
    return rel in (STATUS, META, DEBT) or _is_area_file(rel)


def _parse(rel: str, text: str, keys: set[str] | None) -> tuple[dict, dict]:
    import yaml

    loader = yaml.SafeLoader(text)
    try:
        node = loader.get_single_node()
        doc = loader.construct_document(node) if node is not None else None
    except yaml.YAMLError as exc:
        raise ContractError(f"{rel} is not valid YAML: {exc}") from exc
    finally:
        loader.dispose()
    if not isinstance(doc, dict):
        raise ContractError(f"{rel} did not parse to a mapping")
    if keys is not None and set(doc) - keys:
        raise ContractError(f"{rel}: unknown top-level keys {sorted(set(doc) - keys)}")
    spans: dict = {}
    for key_node, value_node in node.value:
        kind = {"claims": "claim", "debt": "debt"}.get(key_node.value)
        if kind is None or not isinstance(doc[key_node.value], list):
            continue
        for entry, item in zip(doc[key_node.value], value_node.value):
            if isinstance(entry, dict) and isinstance(entry.get("id"), str):
                last = item.end_mark.line + (1 if item.end_mark.column else 0)
                spans.setdefault((kind, entry["id"]), (rel, item.start_mark.line + 1, last))
    return doc, spans


def _assemble(files: list[str], read) -> Contract:
    if files == [STATUS]:
        data, spans = _parse(STATUS, read(STATUS), None)
        return Contract(data, spans)
    data, origin = _parse(META, read(META), _KEYS[META])
    contract = Contract(data, origin)
    claims: list = []
    for rel in files[1:]:
        doc, spans = _parse(rel, read(rel), _KEYS.get(rel, _AREA_KEYS))
        for key, where in spans.items():
            if key in contract.origin:
                first = contract.origin[key]
                contract.problems.append(
                    (key[1], f"duplicate claim id in {first[0]}:{first[1]} and {where[0]}:{where[1]}")
                )
            else:
                contract.origin[key] = where
        if rel == DEBT:
            data.update(doc)
            continue
        stem = Path(rel).stem
        if not AREA_NAME_RE.match(stem):
            contract.problems.append(("-", f"{rel}: area file name must match {AREA_NAME_RE.pattern}"))
        if doc.get("area") != stem:
            contract.problems.append(
                ("-", f"{rel}: area {doc.get('area')!r} does not match the file name {stem!r}")
            )
        if not isinstance(doc.get("claims", []), list):
            raise ContractError(f"{rel}: claims must be a list")
        for claim in doc.get("claims") or []:
            if isinstance(claim, dict) and isinstance(claim.get("id"), str):
                contract.area_of_claim.setdefault(claim["id"], stem)
            claims.append(claim)
    data["claims"] = claims
    return contract


def load(root: Path) -> Contract:
    files = contract_files(root)
    if not files:
        raise ContractError(f"{STATUS} not found at {root / STATUS}")
    return _assemble(files, lambda rel: (root / rel).read_text())


def _git(toplevel: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=toplevel, capture_output=True, text=True, timeout=30)


def load_at(toplevel: Path, root: Path, rev: str) -> Contract | None:
    """The contract as committed at rev, whichever layout it had there. None
    when rev has no contract under root (or does not exist)."""
    prefix = root.resolve().relative_to(toplevel.resolve()).as_posix()
    pre = "" if prefix == "." else prefix + "/"
    listed = _git(toplevel, "ls-tree", "-r", "--name-only", rev, "--", pre + STATUS, pre + DIR)
    if listed.returncode != 0:
        return None
    names = {n[len(pre):] for n in listed.stdout.splitlines()}
    files = _files(names.__contains__, [n for n in names if _is_area_file(n)])
    if not files:
        return None

    def read(rel: str) -> str:
        shown = _git(toplevel, "show", f"{rev}:{pre}{rel}")
        if shown.returncode != 0:
            raise ContractError(f"cannot read {rel} at {rev}: {shown.stderr.strip()}")
        return shown.stdout

    return _assemble(files, read)


def area_index(root: Path) -> list[tuple[str, list[str]]]:
    """(area id, sources): model.yaml areas, then shards that carry sources
    for an id model.yaml does not have."""
    import yaml

    model_path = root / MODEL
    model = yaml.safe_load(model_path.read_text()) if model_path.is_file() else None
    areas = model.get("areas") or [] if isinstance(model, dict) else []
    out = [(a["id"], a.get("sources") or []) for a in areas if isinstance(a, dict) and "id" in a]
    known = {area_id for area_id, _ in out}
    for rel in contract_files(root):
        if not _is_area_file(rel):
            continue
        doc = yaml.safe_load((root / rel).read_text())
        if isinstance(doc, dict) and doc.get("sources") and doc.get("area") not in known:
            out.append((doc["area"], doc["sources"]))
    return out


def enforcement(root: Path) -> str:
    """blocking when the contract meta or model.yaml says so, else advisory."""
    import yaml

    files = contract_files(root)
    sources = [(files[0], lambda d: (d.get("meta") or {}).get("enforcement"))] if files else []
    sources.append((MODEL, lambda d: d.get("enforcement")))
    for rel, get in sources:
        path = root / rel
        data = yaml.safe_load(path.read_text()) if path.is_file() else None
        if isinstance(data, dict) and get(data) == "blocking":
            return "blocking"
    return "advisory"


def parse_ref(s: str) -> dict:
    """claim:<id> or STATUS.yaml#<id> -> {claim}; path::symbol -> {path, symbol};
    path:N[-M] «quote» -> {path, start, end, quote}; anything else -> {path}."""
    s = s.strip()
    m = CLAIM_REF_RE.fullmatch(s)
    if m:
        return {"claim": m.group(1)}
    path, sep, symbol = s.partition("::")
    if sep:
        return {"path": path, "symbol": symbol}
    m = _LINES_REF_RE.fullmatch(s)
    if m:
        start = int(m["start"])
        return {"path": m["path"], "start": start, "end": int(m["end"] or start), "quote": m["quote"]}
    return {"path": s}


def format_ref(ref: dict) -> str:
    if "claim" in ref:
        return f"claim:{ref['claim']}"
    if "symbol" in ref:
        return f"{ref['path']}::{ref['symbol']}"
    if "start" in ref:
        span = str(ref["start"]) if ref["end"] == ref["start"] else f"{ref['start']}-{ref['end']}"
        quote = f" «{ref['quote']}»" if ref.get("quote") else ""
        return f"{ref['path']}:{span}{quote}"
    return ref["path"]


def write_meta(root: Path, updates: dict) -> None:
    """Set meta.<key>: <value> in the file that holds meta, rewriting only
    those lines; a key not there yet is added at the end of the meta block."""
    files = contract_files(root)
    if not files:
        raise ContractError(f"{STATUS} not found at {root / STATUS}")
    path = root / files[0]
    lines = path.read_text().splitlines(keepends=True)
    start = next((i for i, line in enumerate(lines) if line.rstrip() == "meta:"), None)
    if start is None:
        raise ContractError(f"{files[0]} has no top-level meta: block")
    end = start + 1
    while end < len(lines) and (not lines[end].strip() or lines[end][:1].isspace()):
        end += 1
    body = [i for i in range(start + 1, end) if lines[i].strip()]
    if not body:
        raise ContractError(f"{files[0]}: meta block is empty")
    indent = re.match(r"[ \t]*", lines[body[0]]).group()
    for key, value in updates.items():
        pat = re.compile(rf"^({indent}{re.escape(key)}:[ \t]*)(['\"]?)[^'\"#\n]*?\2([ \t]*(?:#.*)?)$")
        for i in body:
            m = pat.match(lines[i].rstrip("\n"))
            if m:
                lines[i] = f"{m[1]}{m[2]}{value}{m[2]}{m[3]}\n"
                break
        else:
            lines.insert(body[-1] + 1, f'{indent}{key}: "{value}"\n')
            body.append(body[-1] + 1)
    path.write_text("".join(lines))
