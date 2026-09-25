# Status contract verifier library. Copied from the ground-truth skill templates; keep in sync with the copy there.
from __future__ import annotations

import ast
import datetime
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

import yaml


class ContractError(Exception):
    """STATUS.yaml is missing or does not parse as a mapping."""


@dataclass
class Issue:
    severity: str  # "fail" | "warn"
    claim_id: str
    message: str


_STATUS_VOCAB = {"implemented", "partial", "stub", "absent", "design-only"}
_CHECK_KINDS = {"pytest", "go_test", "js_test", "junit", "probe"}
_DEBT_SEVERITY = {"blocking", "normal", "cosmetic"}
_DEBT_STATE = {"open", "accepted", "resolved"}
_DEBT_ALLOWED_KEYS = {"id", "ref", "description", "severity", "state", "owner", "opened"}
_DEFAULT_EXTENSIONS = [".py", ".go", ".ts", ".tsx", ".js", ".sh", ".kt", ".java"]
# Docs, media, data and lock files never hold code a claim would describe;
# the WARN about extensions outside meta.coverage.extensions leaves them out
# so the source files a repo's own language adds stay visible.
_NON_CODE_EXTENSIONS = frozenset(
    ".md .mdx .rst .adoc .txt .png .jpg .jpeg .gif .webp .svg .ico .pdf .mp3 .mp4 "
    ".woff .woff2 .ttf .otf .eot .json .yml .yaml .toml .lock .csv .xml .ini .cfg "
    ".env .example .gitkeep .map".split()
)
# What counts as a test file. A test carries no claim of its own -- it is
# what a claim points at. gt_edit_guard.py keeps its own copy of these
# constants and of _NON_CODE_EXTENSIONS (it must not import this module, see
# its is_test_file); the selftest fails when the copies differ.
# tests/ and __tests__/ count anywhere in the path; test/ and spec/ only as
# the first segment or right after src/, so src/api/spec/ stays code.
_TEST_DIR_NAMES = ("tests", "__tests__")
_TEST_TOP_DIR_NAMES = ("test", "spec")
_TEST_NAME_MARKERS = (".test.", ".spec.")
_TEST_SUFFIX_RE = re.compile(r"_test\.[^./]+$")
_STALE_DEFAULT_DAYS = 30
# An audit older than this many days, or taken more than this many files ago,
# no longer describes the repository it was written against.
_AUDIT_MAX_DAYS = 30
_AUDIT_MAX_FILES = 50
_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{7,40}$")
# edge.to external-repo shape: "<repo/domain>:<free text>", any prefix.
_EXTERNAL_EDGE_RE = re.compile(r"^[A-Za-z0-9_.\-]+:.+$")
# path:N or path:A-B citation inside a claim's note -- path either has a "/"
# or a dotted extension, per check_cited_lines.
_CITE_RE = re.compile(
    r"(?P<path>(?:[\w.\-]+/)+[\w.\-]+|[\w\-]+\.[A-Za-z0-9]{1,5}):(?P<line>\d+)"
    r"(?:-(?P<line_end>\d+))?(?!\d)"
)
_QUOTE_RE = re.compile(r"«([^»]+)»|\"([^\"]+)\"|`([^`]+)`")

_KIND_FIELDS = {
    "status": {
        "required": {"status", "check_kind", "check"},
        "forbidden": {"command", "pointer"},
    },
    "live_state": {
        "required": {"command"},
        "forbidden": {"status", "check_kind", "check", "pointer"},
    },
    "out_of_repo": {
        "required": {"pointer"},
        "forbidden": {"status", "check_kind", "check", "command"},
    },
}

# Identity tokens: a repo carrying these reads as assisted rather than
# ordinary engineering work. Entries below are scanner data, not prose
# about the tool itself, hence the per-line escape marker.
_FAIL_PATTERNS = [
    re.compile(p)
    for p in (
        r"\bClaude\b",  # gt-allow: scanner pattern, not prose
        r"\bAnthropic\b",  # gt-allow: scanner pattern, not prose
        r"\bChatGPT\b",  # gt-allow: scanner pattern, not prose
        r"\bGPT-?\d",  # gt-allow: scanner pattern, not prose
        r"\bCopilot\b",  # gt-allow: scanner pattern, not prose
        r"\bLLM\b",  # gt-allow: scanner pattern, not prose
        r"Co-Authored-By",  # gt-allow: scanner pattern, not prose
        r"\bAI\b",  # gt-allow: scanner pattern, not prose
        r"AI-assisted",  # gt-allow: scanner pattern, not prose
        r"AI-generated",  # gt-allow: scanner pattern, not prose
        r"coding agent",  # gt-allow: scanner pattern, not prose
        r"language model",  # gt-allow: scanner pattern, not prose
    )
]

_WARN_WORDS = (
    "delve",  # gt-allow: scanner word list, not prose
    "leverage",  # gt-allow: scanner word list, not prose
    "comprehensive",  # gt-allow: scanner word list, not prose
    "robust",  # gt-allow: scanner word list, not prose
    "seamless",  # gt-allow: scanner word list, not prose
    "streamline",  # gt-allow: scanner word list, not prose
    "consolidate",  # gt-allow: scanner word list, not prose
    "modernize",  # gt-allow: scanner word list, not prose
    "enhanced",  # gt-allow: scanner word list, not prose
    "utilize",  # gt-allow: scanner word list, not prose
    "facilitate",  # gt-allow: scanner word list, not prose
)
_WARN_PATTERNS = [re.compile(r"\b" + w + r"\b", re.IGNORECASE) for w in _WARN_WORDS]

# Representative paths standing in for the contract's own artifacts. A
# meta.banned_words.exclude glob is rejected in check_shape if it would
# match any of these -- the scan exists to keep the contract's own files
# clean, so it must never be told to skip them.
_PROTECTED_ARTIFACT_PROBES = (
    "STATUS.yaml",
    "tools/ground_truth/contract_lib.py",
    "tools/ground_truth/verify.py",
    "tools/ground_truth/probes/example_probe.py",
    "tests/test_status_contract.py",
    "tests/sub/test_nested.py",
)


def is_test_path(rel: str) -> bool:
    """True for a repo-relative path that is itself a test: under a tests or
    __tests__ directory anywhere, under test/ or spec/ as the first segment
    or right after src/, or a code file (extension outside
    _NON_CODE_EXTENSIONS) named test_*, *_test.<ext>, *.test.* or *.spec.*."""
    parts = Path(rel).as_posix().split("/")
    dirs = parts[:-1]
    if any(p in _TEST_DIR_NAMES for p in dirs):
        return True
    for i, p in enumerate(dirs):
        if p in _TEST_TOP_DIR_NAMES and (i == 0 or dirs[i - 1] == "src"):
            return True
    name = parts[-1]
    if Path(name).suffix.lower() in _NON_CODE_EXTENSIONS:
        return False
    if name.startswith("test_") or _TEST_SUFFIX_RE.search(name):
        return True
    return any(m in name for m in _TEST_NAME_MARKERS)


def _covers_contract_artifacts(pattern: str) -> bool:
    return any(fnmatch.fnmatch(probe, pattern) for probe in _PROTECTED_ARTIFACT_PROBES)


def load_contract(root: Path) -> dict:
    path = root / "STATUS.yaml"
    if not path.is_file():
        raise ContractError(f"STATUS.yaml not found at {path}")
    try:
        data = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise ContractError(f"STATUS.yaml is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ContractError("STATUS.yaml did not parse to a mapping")
    return data


def _under_human_docs(rel: str) -> bool:
    # Normalize lexically first (os.path.normpath needs no filesystem access
    # and does not require the target to exist) so a ".."-traversal such as
    # "docs/other/../_human/x.py" cannot re-enter the quarantine undetected.
    norm = os.path.normpath(Path(rel).as_posix())
    parts = Path(norm).as_posix().split("/")
    for i in range(len(parts) - 1):
        if parts[i] == "docs" and parts[i + 1] == "_human":
            return True
    return False


class GitUnavailable(RuntimeError):
    """git ls-files could not be run or exited nonzero.

    Callers must turn this into a fail Issue, never treat it the same as
    "git ran and found zero tracked files" -- a scan that could not run is
    not the same thing as a scan that found nothing.
    """


def _git_ls_files(root: Path, include_untracked: bool = False) -> list[str]:
    """Tracked files by default; also untracked-but-not-ignored files when
    include_untracked is set, so a banned word sitting in a new file is caught
    before that file is ever `git add`-ed."""
    args = (
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"]
        if include_untracked
        else ["git", "ls-files", "-z"]
    )
    try:
        result = subprocess.run(
            args, cwd=root, capture_output=True, text=True, errors="surrogateescape", timeout=30
        )
    except OSError as exc:
        raise GitUnavailable(f"could not run git ls-files: {exc}") from exc
    if result.returncode != 0:
        raise GitUnavailable(
            f"git ls-files failed (exit {result.returncode}): {result.stderr.strip()}"
        )
    seen: set[str] = set()
    files: list[str] = []
    for line in result.stdout.split("\0"):
        if line and line not in seen:
            seen.add(line)
            files.append(line)
    return files


def check_shape(root: Path, data: dict) -> list[Issue]:
    issues: list[Issue] = []

    if data.get("schema_version") != 1:
        issues.append(
            Issue("fail", "-", f"schema_version must be 1, got {data.get('schema_version')!r}")
        )

    meta = data.get("meta")
    if not isinstance(meta, dict):
        issues.append(Issue("fail", "-", "meta section missing or not a mapping"))
        meta = {}
    else:
        if not isinstance(meta.get("repo"), str) or not meta["repo"]:
            issues.append(Issue("fail", "-", "meta.repo must be a non-empty string"))
        if meta.get("enforcement") not in ("advisory", "blocking"):
            issues.append(
                Issue(
                    "fail",
                    "-",
                    f"meta.enforcement must be advisory or blocking, got {meta.get('enforcement')!r}",
                )
            )
        if not _DATE_RE.match(str(meta.get("last_audited", ""))):
            issues.append(
                Issue(
                    "fail",
                    "-",
                    f"meta.last_audited must be YYYY-MM-DD, got {meta.get('last_audited')!r}",
                )
            )
        audited_commit = meta.get("last_audited_commit")
        if audited_commit is not None and not _COMMIT_RE.match(str(audited_commit)):
            issues.append(
                Issue(
                    "fail",
                    "-",
                    "meta.last_audited_commit must be a 7-40 character commit sha, "
                    f"got {audited_commit!r}",
                )
            )

        coverage = meta.get("coverage")
        if not isinstance(coverage, dict):
            issues.append(Issue("fail", "-", "meta.coverage section missing or not a mapping"))
        else:
            if not isinstance(coverage.get("roots"), list) or not coverage["roots"]:
                issues.append(Issue("fail", "-", "meta.coverage.roots must be a non-empty list"))
            if not isinstance(coverage.get("exclude", []), list):
                issues.append(Issue("fail", "-", "meta.coverage.exclude must be a list"))
            if coverage.get("runner") not in ("pytest", "go_test", "js_test", "junit", "none"):
                issues.append(
                    Issue(
                        "fail",
                        "-",
                        f"meta.coverage.runner must be one of pytest/go_test/js_test/junit/none, got {coverage.get('runner')!r}",
                    )
                )
            if "extensions" in coverage and not isinstance(coverage["extensions"], list):
                issues.append(
                    Issue("fail", "-", "meta.coverage.extensions must be a list when present")
                )

        banned_words_cfg = meta.get("banned_words")
        if banned_words_cfg is not None:
            if not isinstance(banned_words_cfg, dict):
                issues.append(Issue("fail", "-", "meta.banned_words must be a mapping"))
            else:
                for key in sorted(set(banned_words_cfg) - {"exclude", "enabled"}):
                    issues.append(
                        Issue("fail", "-", f"meta.banned_words has unknown key {key!r}")
                    )
                if "enabled" in banned_words_cfg and not isinstance(
                    banned_words_cfg["enabled"], bool
                ):
                    issues.append(
                        Issue("fail", "-", "meta.banned_words.enabled must be true or false")
                    )
                exclude_cfg = banned_words_cfg.get("exclude")
                if exclude_cfg is not None and not isinstance(exclude_cfg, list):
                    issues.append(Issue("fail", "-", "meta.banned_words.exclude must be a list"))
                elif isinstance(exclude_cfg, list):
                    for entry in exclude_cfg:
                        if not isinstance(entry, dict):
                            issues.append(
                                Issue(
                                    "fail",
                                    "-",
                                    "meta.banned_words.exclude entries must be mappings",
                                )
                            )
                            continue
                        path = entry.get("path")
                        why = entry.get("why")
                        if not isinstance(path, str) or not path:
                            issues.append(
                                Issue(
                                    "fail",
                                    "-",
                                    "meta.banned_words.exclude entry must have a non-empty "
                                    "string 'path'",
                                )
                            )
                            path = None
                        if not isinstance(why, str) or not why:
                            issues.append(
                                Issue(
                                    "fail",
                                    "-",
                                    f"meta.banned_words.exclude entry {path!r} must have a "
                                    "non-empty string 'why'",
                                )
                            )
                        if isinstance(path, str) and path and _covers_contract_artifacts(path):
                            issues.append(
                                Issue(
                                    "fail",
                                    "-",
                                    "banned_words.exclude may not cover contract artifacts: "
                                    f"{path}",
                                )
                            )

    claims = data.get("claims")
    if not isinstance(claims, list) or not claims:
        issues.append(Issue("fail", "-", "claims must be a non-empty list"))
        claims = []

    seen_ids: set[str] = set()
    for claim in claims:
        if not isinstance(claim, dict):
            issues.append(Issue("fail", "-", "claim entries must be mappings"))
            continue
        cid = claim.get("id")
        if not isinstance(cid, str) or not _ID_RE.match(cid):
            issues.append(Issue("fail", str(cid), "id must be a snake_case string"))
            cid = str(cid)
        if cid in seen_ids:
            issues.append(Issue("fail", cid, "duplicate claim id"))
        seen_ids.add(cid)

        if not isinstance(claim.get("component"), str) or not claim["component"]:
            issues.append(Issue("fail", cid, "component must be a non-empty string"))

        if not isinstance(claim.get("note"), str) or not claim["note"]:
            issues.append(Issue("fail", cid, "note must be a non-empty string"))

        kind = claim.get("kind")
        rule = _KIND_FIELDS.get(kind)
        if rule is None:
            issues.append(Issue("fail", cid, f"unknown kind {kind!r}"))
            continue
        for key in rule["required"]:
            if key not in claim:
                issues.append(Issue("fail", cid, f"missing {key!r} for kind={kind}"))
        for key in rule["forbidden"]:
            if key in claim:
                issues.append(Issue("fail", cid, f"{key!r} not allowed for kind={kind}"))

        if kind == "status":
            if claim.get("status") not in _STATUS_VOCAB:
                issues.append(
                    Issue(
                        "fail",
                        cid,
                        f"status must be one of {sorted(_STATUS_VOCAB)}, got {claim.get('status')!r}",
                    )
                )
            if claim.get("check_kind") not in _CHECK_KINDS:
                issues.append(
                    Issue(
                        "fail",
                        cid,
                        f"check_kind must be one of {sorted(_CHECK_KINDS)}, got {claim.get('check_kind')!r}",
                    )
                )
            if not isinstance(claim.get("check"), str) or not claim["check"]:
                issues.append(Issue("fail", cid, "check must be a non-empty string"))
            if claim.get("canary"):
                mutation = claim.get("mutation")
                if not isinstance(mutation, dict) or not all(
                    k in mutation for k in ("file", "find", "replace")
                ):
                    issues.append(
                        Issue("fail", cid, "canary: true requires mutation.file/find/replace")
                    )

        if "path" in claim:
            raw_paths = claim["path"]
            paths = [raw_paths] if isinstance(raw_paths, str) else raw_paths
            if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
                issues.append(Issue("fail", cid, "path must be a string or list of strings"))
                paths = []
            for p in paths:
                if _under_human_docs(p):
                    issues.append(Issue("fail", cid, f"path {p!r} resolves inside docs/_human/"))

        if isinstance(claim.get("check"), str) and claim.get("check_kind") in ("pytest", "probe"):
            for part in claim["check"].split(";"):
                part = part.strip()
                check_path = part.split("::", 1)[0]
                if _under_human_docs(check_path):
                    issues.append(
                        Issue("fail", cid, f"check {part!r} resolves inside docs/_human/")
                    )

        for ref_key in ("depends_on", "see_also"):
            if ref_key in claim and not isinstance(claim[ref_key], list):
                issues.append(Issue("fail", cid, f"{ref_key} must be a list of claim ids"))

    debt = data.get("debt")
    if debt is not None and not isinstance(debt, list):
        issues.append(Issue("fail", "-", "debt must be a list"))
        debt = []
    for entry in debt or []:
        if not isinstance(entry, dict):
            issues.append(Issue("fail", "-", "debt entries must be mappings"))
            continue
        did = entry.get("id")
        if not isinstance(did, str) or not did:
            issues.append(Issue("fail", "-", "debt entry missing id"))
            did = "-"
        for key in entry:
            if key not in _DEBT_ALLOWED_KEYS:
                issues.append(
                    Issue(
                        "fail",
                        did,
                        f"debt {did}: unknown key {key!r} (allowed: {sorted(_DEBT_ALLOWED_KEYS)})",
                    )
                )
        if not isinstance(entry.get("ref"), str) or not entry["ref"]:
            issues.append(Issue("fail", did, "debt entry missing ref"))
        if not isinstance(entry.get("description"), str) or not entry["description"]:
            issues.append(Issue("fail", did, "debt entry missing description"))
        if "owner" in entry and not isinstance(entry["owner"], str):
            issues.append(Issue("fail", did, "debt.owner must be a string"))
        if not _DATE_RE.match(str(entry.get("opened", ""))):
            issues.append(
                Issue("fail", did, f"debt.opened must be YYYY-MM-DD, got {entry.get('opened')!r}")
            )

    edges = data.get("edges")
    if edges is not None and not isinstance(edges, list):
        issues.append(Issue("fail", "-", "edges must be a list"))
        edges = []
    for edge in edges or []:
        if not isinstance(edge, dict):
            issues.append(Issue("fail", "-", "edge entries must be mappings"))
            continue
        for key in ("from", "to", "probe"):
            if key not in edge or not isinstance(edge[key], str) or not edge[key]:
                issues.append(Issue("fail", "-", f"edge missing {key!r}"))

    return issues


def _root_prefix(r) -> str | None:
    """One meta.coverage.roots entry as a repo-relative posix prefix, or None
    when the entry cannot be one. An empty string means the repository root,
    which every path is under.

    Roots are hand-written, so the same directory arrives as "app", "./app",
    "app/" or "." -- every reader of meta.coverage.roots normalizes here so
    two of them can never disagree about what the contract covers.
    """
    if not isinstance(r, str):
        return None
    cleaned = r.strip().strip("/")
    if not cleaned:
        return ""
    prefix = Path(os.path.normpath(cleaned)).as_posix()
    if prefix == ".":
        return ""
    if prefix == ".." or prefix.startswith("../"):
        return None
    return prefix


def _path_in_root(rel: str, prefix: str) -> bool:
    """rel sits at or below a normalized root prefix ("" = repository root)."""
    return prefix == "" or rel == prefix or rel.startswith(prefix + "/")


def _under_coverage_roots(rel: str, roots: list, excludes: list) -> bool:
    """True when a repo-relative path sits at or below one of
    meta.coverage.roots and no exclude glob matches it. The coverage scan and
    the audit-due file count both need to answer "is this file part of what
    the contract covers", and they must answer it the same way -- a file the
    coverage scan ignores cannot make an audit look stale."""
    inside = False
    for r in roots:
        prefix = _root_prefix(r)
        if prefix is None:
            continue
        if _path_in_root(rel, prefix):
            inside = True
            break
    if not inside:
        return False
    return not any(fnmatch.fnmatch(rel, pat) for pat in excludes if isinstance(pat, str))


def _missing_path_hint(root: Path, rel: str, cid: str) -> str:
    """sync-mode fix hint for a claim path that no longer exists: was it
    renamed or deleted (git diff for an uncommitted change, git log for a
    committed one), or did git not answer at all."""
    try:
        cps = [
            _git_run(root, ["diff", "--name-status", "-M", "HEAD"]),
            _git_run(
                root,
                ["log", "--diff-filter=DR", "-M", "--name-status", "--format=", "-n", "20"],
            ),
        ]
    except GitUnavailable:
        cps = []
    for cp in cps:
        if cp.returncode != 0:
            continue
        for line in cp.stdout.splitlines():
            fields = line.split("\t")
            if len(fields) < 2 or fields[1] != rel:
                continue
            if fields[0].startswith("R") and len(fields) >= 3:
                return f"renamed to {fields[2]}; fix: set path: {fields[2]} in claim {cid}"
            if fields[0] == "D":
                return f"deleted; fix: drop the path from claim {cid} or mark status absent"
    return f"fix: update path: in claim {cid}"


def check_coverage(root: Path, data: dict, mode: str = "full") -> list[Issue]:
    issues: list[Issue] = []
    meta = data.get("meta", {}) or {}
    coverage_cfg = meta.get("coverage", {}) or {}
    roots = coverage_cfg.get("roots") or []
    excludes = coverage_cfg.get("exclude") or []
    extensions = coverage_cfg.get("extensions") or _DEFAULT_EXTENSIONS
    claims = data.get("claims") or []
    root_resolved = root.resolve()

    coverage_roots_resolved: list[Path] = []
    for r in roots:
        base = root / r
        if base.exists():
            coverage_roots_resolved.append(base.resolve())

    claim_paths: list[tuple[str, Path]] = []
    for claim in claims:
        if not isinstance(claim, dict):
            continue
        cid = claim.get("id", "-")
        raw_paths = claim.get("path")
        if raw_paths is None:
            continue
        paths = [raw_paths] if isinstance(raw_paths, str) else raw_paths
        if not isinstance(paths, list):
            continue
        for p in paths:
            if not isinstance(p, str):
                continue
            full = root / p
            if not full.exists():
                if mode == "sync":
                    hint = _missing_path_hint(root, p, cid)
                    issues.append(Issue("warn", cid, f"path {p!r} does not exist; {hint}"))
                else:
                    issues.append(Issue("fail", cid, f"path {p!r} does not exist"))
                continue
            full_resolved = full.resolve()
            if full_resolved == root_resolved:
                issues.append(
                    Issue(
                        "fail",
                        cid,
                        f"path {p!r} resolves to the repo root itself -- a claim cannot "
                        "cover the whole repository, it defeats coverage two-way",
                    )
                )
                continue
            if any(full_resolved in cr.parents for cr in coverage_roots_resolved):
                issues.append(
                    Issue(
                        "fail",
                        cid,
                        f"path {p!r} is an ancestor of a coverage root -- a claim path "
                        "must be at or below meta.coverage.roots, never above it",
                    )
                )
                continue
            claim_paths.append((cid, full_resolved))

    covered_paths = {p for _, p in claim_paths}
    covered_dirs = {p for _, p in claim_paths if p.is_dir()}

    # A candidate file is resolved, so under a symlinked root ("src" ->
    # "real/src") its repo-relative path spells the real directory and the
    # written root never matches it -- the scan would then find nothing
    # uncovered under that root at all. Both spellings count.
    roots_expanded = list(roots)
    for r in roots:
        prefix = _root_prefix(r)
        if prefix is None:
            continue
        resolved = (root_resolved / prefix).resolve()
        if resolved == root_resolved or root_resolved in resolved.parents:
            roots_expanded.append(resolved.relative_to(root_resolved).as_posix())
    prefixes = [p for p in (_root_prefix(r) for r in roots_expanded) if p is not None]

    # git decides what is part of the repository: tracked files plus untracked
    # ones that are not ignored. A walk of the file system would also list
    # node_modules, build output and nested worktrees.
    try:
        listed = _git_ls_files(root, include_untracked=True)
    except GitUnavailable as exc:
        issues.append(
            Issue("fail", "-", f"could not enumerate files, coverage scan did not run: {exc}")
        )
        return issues

    candidate_files: list[Path] = []
    other_ext: list[tuple[str, str]] = []
    for rel in listed:
        if not any(_path_in_root(rel, prefix) for prefix in prefixes):
            continue
        rel_parts = rel.split("/")
        if "__pycache__" in rel_parts or ".worktrees" in rel_parts:
            continue
        # The contract's own tree (verifier, probes, hooks) is never a
        # coverage unit. A flat repository with roots ["."] would otherwise
        # have to exclude it by glob, and that glob trips the 90% guard.
        if rel_parts[:2] == ["tools", "ground_truth"]:
            continue
        # git lists an untracked nested repository (or a submodule's
        # checkout) as one entry ending in "/" and never looks inside it.
        if rel.endswith("/"):
            issues.append(
                Issue("warn", "-", f"nested repository under roots not scanned: {rel.rstrip('/')}")
            )
            continue
        if is_test_path(rel):
            continue
        f = root / rel
        if not f.is_file():
            continue
        if f.suffix not in extensions:
            if f.suffix and f.suffix.lower() not in _NON_CODE_EXTENSIONS:
                other_ext.append((rel, f.suffix))
            continue
        candidate_files.append(f.resolve())

    # fnmatch has no path-segment boundary ("*"/"**" translate the same way),
    # so a bare wildcard exclude silently swallows the whole coverage scan.
    # Reject that shape outright, and FAIL any exclude pattern that matches
    # almost every candidate file even if it isn't literally bare.
    str_excludes = [e for e in excludes if isinstance(e, str)]
    for pat in str_excludes:
        if not pat.strip("*"):
            issues.append(
                Issue(
                    "fail",
                    "-",
                    f"meta.coverage.exclude entry {pat!r} is a bare wildcard -- fnmatch "
                    "has no path-segment boundary, this would silently exclude every "
                    "file from coverage; use a pattern with a literal path prefix",
                )
            )
            continue
        if candidate_files:
            matched = sum(
                1
                for f in candidate_files
                if fnmatch.fnmatch(f.relative_to(root_resolved).as_posix(), pat)
            )
            if matched / len(candidate_files) > 0.9:
                issues.append(
                    Issue(
                        "fail",
                        "-",
                        f"meta.coverage.exclude entry {pat!r} matches {matched}/"
                        f"{len(candidate_files)} candidate files -- refine it, a single "
                        "exclude should not swallow nearly the entire coverage scan",
                    )
                )

    active_excludes = [pat for pat in str_excludes if pat.strip("*")]

    # Files under the roots that the extension filter drops are invisible to
    # the scan. Say how many there are once, so a language missing from
    # meta.coverage.extensions shows up instead of reading as full coverage.
    ext_counts: dict[str, int] = {}
    for rel, suffix in other_ext:
        if _under_coverage_roots(rel, roots_expanded, active_excludes):
            ext_counts[suffix] = ext_counts.get(suffix, 0) + 1
    if ext_counts:
        top = sorted(ext_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:5]
        issues.append(
            Issue(
                "warn",
                "-",
                f"{sum(ext_counts.values())} files under roots have extensions "
                "outside meta.coverage.extensions (top: "
                + ", ".join(f"{ext}\u00d7{n}" for ext, n in top)
                + ")",
            )
        )

    units: list[Path] = []
    for f in candidate_files:
        rel = f.relative_to(root_resolved).as_posix()
        if not _under_coverage_roots(rel, roots_expanded, active_excludes):
            continue
        units.append(f)

    for unit in units:
        if unit in covered_paths:
            continue
        if any(parent in covered_dirs for parent in unit.parents):
            continue
        rel = unit.relative_to(root_resolved).as_posix()
        if mode == "sync":
            issues.append(
                Issue(
                    "warn",
                    "-",
                    f"{rel} is not covered by any claim; fix: add a claim with path: {rel} "
                    "or add it to meta.coverage.exclude",
                )
            )
        else:
            issues.append(Issue("fail", "-", f"{rel} is not covered by any claim"))

    return issues


def _within_root(root: Path, rel: str) -> Path | None:
    """Path of rel under root, or None when it escapes. Paths here come from
    STATUS.yaml -- a note citation or a check nodeid -- and a ".." segment in
    one of them otherwise reads a file outside the repository."""
    full = (root / rel).resolve()
    try:
        full.relative_to(root.resolve())
    except ValueError:
        return None
    return full


def _resolve_pytest_static(root: Path, nodeid: str) -> tuple[bool, str]:
    path_part, _, qual = nodeid.partition("::")
    full = _within_root(root, path_part)
    if full is None:
        return False, f"{nodeid}: {path_part} is outside the repository"
    if not full.is_file():
        return False, f"{nodeid}: {path_part} does not exist"
    try:
        tree = ast.parse(full.read_text(), filename=str(full))
    except SyntaxError as exc:
        return False, f"{nodeid}: {path_part} does not parse: {exc}"
    if not qual:
        return True, ""
    scope: list[ast.stmt] = tree.body
    node = None
    for part in qual.split("::"):
        node = None
        for candidate in scope:
            if (
                isinstance(candidate, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                and candidate.name == part
            ):
                node = candidate
                break
        if node is None:
            return False, f"{nodeid}: {part!r} not found in {path_part}"
        scope = node.body
    return True, ""


def _resolve_go_test(root: Path, nodeid: str) -> tuple[bool, str]:
    pkg, sep, name = nodeid.partition("::")
    if not sep:
        return False, f"{nodeid}: expected pkg::TestName"
    pkg_dir = root / pkg
    if not pkg_dir.is_dir():
        return False, f"{nodeid}: {pkg} is not a directory"
    pattern = re.compile(r"^func\s+" + re.escape(name) + r"\(", re.MULTILINE)
    for f in pkg_dir.glob("*_test.go"):
        if pattern.search(f.read_text()):
            return True, ""
    return False, f"{nodeid}: no func {name} found in {pkg}/*_test.go"


def _maven_modules(root: Path) -> list[Path]:
    """Directories a JUnit check can resolve against: the repository root
    plus every directory under it with its own pom.xml, skipping build/VCS
    output where a stray pom.xml would be noise, not a real module."""
    modules = [root]
    skip = {"target", ".git", "node_modules"}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in skip)
        if "pom.xml" in filenames and Path(dirpath) != root:
            modules.append(Path(dirpath))
    return modules


def _resolve_junit(root: Path, part: str) -> tuple[bool, str, Path | None]:
    fqn, _, method = part.partition("::")
    file_rel = Path(*fqn.split(".")).with_suffix(".java")
    pattern = re.compile(r"\bvoid\s+" + re.escape(method) + r"\s*\(")
    for module_dir in _maven_modules(root):
        candidate = module_dir / "src" / "test" / "java" / file_rel
        if candidate.is_file():
            if pattern.search(candidate.read_text()):
                return True, "", module_dir
            return False, f"junit check not found: {part}", None
    return False, f"no pom.xml module holds {file_rel.as_posix()}", None


def _surefire_outcome(report_path: Path, fqn: str, method: str) -> tuple[bool, str]:
    if not report_path.is_file():
        return False, f"surefire report missing for {fqn}: test did not run"
    try:
        tree = ET.parse(report_path)
    except ET.ParseError as exc:
        return False, f"surefire report unparsable for {fqn}: {exc}"
    testcase = None
    for tc in tree.getroot().iter("testcase"):
        if tc.get("classname") != fqn:
            continue
        name = tc.get("name") or ""
        if name == method or name.startswith(method + "(") or name.startswith(method + "["):
            testcase = tc
            break
    if testcase is None:
        return False, f"testcase {method} not in surefire report: test did not run"
    failure = testcase.find("failure")
    if failure is None:
        failure = testcase.find("error")
    if failure is not None:
        return False, failure.get("message") or ""
    if testcase.find("skipped") is not None:
        return False, "test skipped"
    return True, ""


def _resolve_js_test(root: Path, nodeid: str) -> tuple[bool, str]:
    path_part, _, name = nodeid.partition("::")
    full = _within_root(root, path_part)
    if full is None:
        return False, f"{nodeid}: {path_part} is outside the repository"
    if not full.is_file():
        return False, f"{nodeid}: {path_part} does not exist"
    text = full.read_text()
    pattern = re.compile(r"(?:test|it|describe)\(\s*(['\"])" + re.escape(name) + r"\1")
    if pattern.search(text):
        return True, ""
    # Hand-rolled runners (a local check("...", cond) helper, tape-style
    # t.test, etc.) do not match the three names above; the test name as an
    # exact quoted string literal anywhere in the file still pins the node.
    literal = re.compile(r"(['\"])" + re.escape(name) + r"\1")
    if literal.search(text):
        return True, ""
    return False, f"{nodeid}: {name!r} not found in {path_part}"


def _resolve_probe_static(root: Path, rel_path: str) -> tuple[bool, str]:
    full = _within_root(root, rel_path)
    if full is None:
        return False, f"{rel_path}: probe path is outside the repository"
    if not full.is_file():
        return False, f"{rel_path}: probe file does not exist"
    if os.access(full, os.X_OK):
        return True, ""
    text = full.read_text(errors="ignore")
    if text.startswith("#!"):
        return True, ""
    return False, f"{rel_path}: probe is not executable and has no shebang"


def _run_probe(root: Path, rel_path: str) -> tuple[bool, str, bool]:
    full = _within_root(root, rel_path)
    if full is None:
        return False, f"{rel_path}: probe path is outside the repository", False
    # A python probe runs under the interpreter running the verifier, so the
    # project venv reaches it even when the file is executable: a shebang would
    # resolve python3 from PATH, which is the system interpreter inside a hook
    # or a bare shell and lacks the project's dependencies.
    if full.suffix == ".py":
        argv = [sys.executable, str(full)]
    else:
        argv = [str(full)] if os.access(full, os.X_OK) else [sys.executable, str(full)]
    try:
        result = subprocess.run(argv, cwd=root, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"{rel_path}: failed to execute probe: {exc}", False
    if result.returncode == 77:
        return False, f"{rel_path}: probe skipped (exit 77)", True
    if result.returncode != 0:
        tail = (result.stdout + result.stderr).strip()[-200:]
        return False, f"{rel_path}: probe failed (exit {result.returncode}): {tail}", False
    return True, "", False


def _run_child(root: Path, argv: list[str], env: dict, timeout: int) -> tuple[bool, str, bool]:
    """One child process of a real check run: (ok, message, skipped). Only the
    last 200 characters of its output travel back -- a failing test suite
    prints pages, and the Issue line has to stay readable."""
    try:
        result = subprocess.run(
            argv, cwd=root, capture_output=True, text=True, timeout=timeout, env=env
        )
    except subprocess.TimeoutExpired:
        return False, f"{argv[0]}: timed out after {timeout}s", False
    except OSError as exc:
        return False, f"{argv[0]}: failed to run: {exc}", False
    if result.returncode != 0:
        tail = (result.stdout + result.stderr).strip()[-200:]
        return False, f"exit {result.returncode}: {tail}", False
    return True, "", False


def _go_module_dir(root: Path, pkg: str) -> Path | None:
    """Directory of the go.mod that owns this package, or None when nothing
    above it declares a module. go refuses to run outside a module, and the
    module is not always the repository root -- a Go component living in
    core/ under a repository of playbooks is the common case."""
    current = (root / pkg.strip("/")).resolve()
    root_resolved = root.resolve()
    while True:
        if (current / "go.mod").is_file():
            return current
        if current == root_resolved or current.parent == current:
            return None
        current = current.parent


def _run_pytest(root: Path, parts: list[str], env: dict, timeout: int) -> tuple[bool, str, bool]:
    """One process for selected checks; exit zero alone also includes skipped tests."""
    if os.environ.get("GT_PYTEST_CHECK_ACTIVE") == "1":
        return False, "recursive pytest contract check; bridge tests must use sync mode", False
    with tempfile.TemporaryDirectory(prefix="gt-pytest-report-") as scratch:
        report = Path(scratch) / "results.xml"
        argv = [sys.executable, "-m", "pytest", *dict.fromkeys(parts), "-q",
                "-p", "no:cacheprovider", f"--junitxml={report}"]
        ok, message, skipped = _run_child(root, argv, {**env, "GT_PYTEST_CHECK_ACTIVE": "1"}, timeout)
        if not ok:
            return ok, message, skipped
        try:
            cases = list(ET.parse(report).getroot().iter("testcase"))
        except (OSError, ET.ParseError) as exc:
            return False, f"pytest produced no usable test report: {exc}", False
        if not cases:
            return False, "pytest ran no test cases", False
        if any(c.find("failure") is not None or c.find("error") is not None for c in cases):
            return False, "pytest report contains a failed test", False
        skipped_cases = [c for c in cases if c.find("skipped") is not None]
        if skipped_cases:
            names = ", ".join(c.get("name", "?") for c in skipped_cases[:5])
            return False, f"pytest skipped {len(skipped_cases)} required check(s): {names}", True
        return True, "", False


def _js_argv(root: Path, file_rel: str, name: str) -> tuple[list[str] | None, str]:
    """Command line that runs one js test by name, or (None, reason) when this
    repository offers no way to run it here. The runner comes from
    package.json; a file that pulls in node:test runs under node's own runner,
    which needs nothing installed."""
    runner = None
    pkg = root / "package.json"
    if pkg.is_file():
        try:
            cfg = json.loads(pkg.read_text())
        except (OSError, ValueError):
            cfg = {}
        if isinstance(cfg, dict):
            blob = " ".join(
                str(cfg.get(key) or "")
                for key in ("dependencies", "devDependencies", "scripts")
            )
            if "vitest" in blob:
                runner = "vitest"
            elif "jest" in blob:
                runner = "jest"
    if runner is not None:
        if shutil.which("npx") is None:
            return None, "npx not found"
        if runner == "vitest":
            return ["npx", "--no-install", "vitest", "run", file_rel, "-t", re.escape(name)], ""
        return ["npx", "--no-install", "jest", file_rel, "-t", re.escape(name)], ""
    try:
        text = (root / file_rel).read_text(errors="ignore")
    except OSError:
        text = ""
    if "node:test" in text:
        if shutil.which("node") is None:
            return None, "node not found"
        # --test-name-pattern is a regex: a test name holding "(", "+" or "."
        # would match nothing, node would run zero tests, exit 0, and a red
        # check would read as green.
        return ["node", "--test", f"--test-name-pattern=^{re.escape(name)}$", file_rel], ""
    return None, "no js test runner configured"


def _run_reported(root: Path, argv: list[str], env: dict, timeout: int,
                  runner: str, name: str) -> tuple[bool, str, bool]:
    """Exit zero with zero selected tests is not passing evidence."""
    with tempfile.TemporaryDirectory(prefix="gt-check-report-") as scratch:
        report = Path(scratch) / "results.json"
        if runner == "vitest":
            argv = [*argv, "--reporter=json", f"--outputFile={report}"]
        elif runner == "jest":
            argv = [*argv, "--json", f"--outputFile={report}"]
        elif runner == "node":
            argv = [argv[0], "--test-reporter=tap", *argv[1:]]
        try:
            proc = subprocess.run(argv, cwd=root, env=env, text=True,
                                  capture_output=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, f"{runner} failed to run: {exc}", False
        if proc.returncode:
            return False, f"exit {proc.returncode}: {(proc.stdout + proc.stderr).strip()[-200:]}", False
        try:
            if runner == "go":
                events = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
                terminal = [e for e in events if e.get("Test") == name and
                            e.get("Action") in ("pass", "fail", "skip")]
                passed = bool(terminal) and terminal[-1]["Action"] == "pass"
                skipped = any(e.get("Action") == "skip" and
                              (e.get("Test") == name or str(e.get("Test", "")).startswith(name + "/"))
                              for e in events)
            elif runner == "node":
                passes = re.findall(r"^# pass (\d+)\s*$", proc.stdout, re.M)
                passed = bool(passes) and int(passes[-1]) > 0
                # Node omits filtered tests on newer versions, or marks them
                # "test name does not match pattern" on older versions.
                skipped = any("# SKIP" in line and "test name does not match pattern" not in line
                              for line in proc.stdout.splitlines())
            else:
                data = json.loads(report.read_text())
                selected = [a for suite in data.get("testResults", [])
                            for a in suite.get("assertionResults", [])
                            if name == a.get("title") or name in a.get("fullName", "")]
                passed = bool(selected) and all(a.get("status") == "passed" for a in selected)
                skipped = any(a.get("status") in ("pending", "skipped", "todo", "disabled") for a in selected)
        except (OSError, ValueError, TypeError) as exc:
            return False, f"{runner} produced no usable execution report: {exc}", False
        if skipped:
            return False, f"{runner} skipped a selected check: {name}", True
        if not passed:
            return False, f"{runner} did not report a passing selected check: {name}", False
        return True, "", False


def run_claim_check(root: Path, claim: dict, timeout: int = 180) -> tuple[bool, str, bool]:
    """Run one claim's check for real; return (ok, message, skipped).

    The single place that turns a claim into a command line, so the full-mode
    verifier, the status-raise guard, the Stop hook enforcer and the mutation
    selfcheck all mean the same thing by "this check is green". skipped is
    True when the check could not be run here at all (no toolchain, probe exit
    77) or its selected test was skipped. A skip is neither green nor
    evidence that a mutation went red; full/CI refuse it. message
    is empty on success and carries the tail of the child output otherwise.
    """
    kind = claim.get("check_kind")
    check = claim.get("check")
    if not isinstance(check, str) or not check.strip():
        return False, "claim has no check to run", True
    parts = [p.strip() for p in check.split(";") if p.strip()]
    # pytest keys its rewritten-bytecode cache on the source mtime with
    # one-second resolution, so a mutation and its revert inside the same
    # second can leave a .pyc that outlives the source it was built from.
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}

    if kind == "pytest":
        return _run_pytest(root, parts, env, timeout)

    if kind == "go_test":
        if shutil.which("go") is None:
            return False, "go toolchain not found", True
        for part in parts:
            pkg, _, name = part.partition("::")
            module_dir = _go_module_dir(root, pkg)
            if module_dir is None:
                return False, f"no go.mod above {pkg}", True
            rel = os.path.relpath((root / pkg.strip("/")).resolve(), module_dir)
            argv = ["go", "test", "-json", "-count=1", "-run", f"^{re.escape(name)}$", "./" + rel.replace(os.sep, "/")]
            ok, msg, skipped = _run_reported(module_dir, argv, env, timeout, "go", name)
            if not ok:
                return ok, msg, skipped
        return True, "", False

    if kind == "js_test":
        for part in parts:
            file_rel, _, name = part.partition("::")
            argv, reason = _js_argv(root, file_rel, name)
            if argv is None:
                return False, reason, True
            runner = "node" if argv[0] == "node" else ("vitest" if "vitest" in argv else "jest")
            ok, msg, skipped = _run_reported(root, argv, env, timeout, runner, name)
            if not ok:
                return ok, msg, skipped
        return True, "", False

    if kind == "junit":
        if shutil.which("mvn") is None:
            return False, "maven toolchain not found", True
        for part in parts:
            ok, msg, module_dir = _resolve_junit(root, part)
            if not ok:
                return False, msg, False
            fqn, _, method = part.partition("::")
            simple_class = fqn.rsplit(".", 1)[-1]
            report_path = module_dir / "target" / "surefire-reports" / f"TEST-{fqn}.xml"
            if report_path.is_file():
                report_path.unlink()
            argv = [
                "mvn", "-q", "-B", "-DskipITs=true",
                "-Dsurefire.failIfNoSpecifiedTests=true",
                f"-Dtest={simple_class}#{method}", "test",
            ]
            ok, msg, skipped = _run_child(module_dir, argv, env, timeout)
            if not ok:
                return ok, msg, skipped
            ok, msg = _surefire_outcome(report_path, fqn, method)
            if not ok:
                return False, msg, False
        return True, "", False

    if kind == "probe":
        for part in parts:
            ok, msg, skipped = _run_probe(root, part)
            if not ok:
                return ok, msg, skipped
        return True, "", False

    return False, f"cannot run check_kind {kind!r}", True


def _collect_only(root: Path, nodeids: list[str]) -> list[Issue]:
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", *nodeids],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except OSError as exc:
        return [Issue("fail", "-", f"pytest --collect-only failed to run: {exc}")]
    if result.returncode != 0:
        tail = (result.stdout + result.stderr).strip().splitlines()[-10:]
        return [Issue("fail", "-", "pytest --collect-only failed: " + " | ".join(tail))]
    return []


def check_collectible(root: Path, data: dict, mode: str) -> list[Issue]:
    issues: list[Issue] = []
    pytest_nodeids: list[str] = []
    # (check_kind, file-or-package, test name) already executed: two claims
    # pointing at the same test must not pay for it twice.
    already_run: set[tuple[str, str, str]] = set()

    def run_once(cid: str, kind: str, part: str) -> None:
        target, _, name = part.partition("::")
        key = (kind, target, name)
        if key in already_run:
            return
        already_run.add(key)
        ok, msg, skipped = run_claim_check(root, {"check_kind": kind, "check": part})
        if skipped:
            issues.append(Issue("fail", cid, f"{part}: check not verified: {msg}"))
        elif not ok:
            issues.append(Issue("fail", cid, f"{part}: check is red: {msg}"))

    for claim in data.get("claims") or []:
        if not isinstance(claim, dict) or claim.get("kind") != "status":
            continue
        cid = claim.get("id", "-")
        kind = claim.get("check_kind")
        check = claim.get("check")
        if not isinstance(check, str):
            continue
        for part in check.split(";"):
            part = part.strip()
            if not part:
                continue
            if kind == "pytest":
                ok, msg = _resolve_pytest_static(root, part)
                if not ok:
                    issues.append(Issue("fail", cid, msg))
                else:
                    pytest_nodeids.append(part)
            elif kind == "go_test":
                ok, msg = _resolve_go_test(root, part)
                if not ok:
                    issues.append(Issue("fail", cid, msg))
                elif mode == "full":
                    run_once(cid, kind, part)
            elif kind == "js_test":
                ok, msg = _resolve_js_test(root, part)
                if not ok:
                    issues.append(Issue("fail", cid, msg))
                elif mode == "full":
                    run_once(cid, kind, part)
            elif kind == "junit":
                ok, msg, _ = _resolve_junit(root, part)
                if not ok:
                    issues.append(Issue("fail", cid, msg))
                elif mode == "full":
                    run_once(cid, kind, part)
            elif kind == "probe":
                ok, msg = _resolve_probe_static(root, part)
                if not ok:
                    issues.append(Issue("fail", cid, msg))
                elif mode == "full":
                    ok2, msg2, warn = _run_probe(root, part)
                    if warn:
                        issues.append(Issue("fail", cid, f"check not verified: {msg2}"))
                    elif not ok2:
                        issues.append(Issue("fail", cid, msg2))

    if mode == "full" and pytest_nodeids:
        ok, message, _ = _run_pytest(root, pytest_nodeids,
                                    {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}, 180)
        if not ok:
            issues.append(Issue("fail", "-", f"pytest checks not verified: {message}"))

    return issues


def check_links(root: Path, data: dict, mode: str) -> list[Issue]:
    issues: list[Issue] = []
    claims = data.get("claims") or []
    by_id = {c["id"]: c for c in claims if isinstance(c, dict) and isinstance(c.get("id"), str)}

    for claim in claims:
        if not isinstance(claim, dict):
            continue
        cid = claim.get("id", "-")
        for ref_key in ("depends_on", "see_also"):
            for ref in claim.get(ref_key) or []:
                if ref not in by_id:
                    issues.append(
                        Issue("fail", cid, f"{ref_key} references unknown claim {ref!r}")
                    )

    for edge in data.get("edges") or []:
        if not isinstance(edge, dict):
            continue
        src = edge.get("from")
        dst = edge.get("to")
        probe = edge.get("probe")
        if src is not None and src not in by_id:
            issues.append(Issue("fail", "-", f"edge.from references unknown claim {src!r}"))
        # "<repo/domain>:<free text>" is accepted for any external prefix, not
        # only the literal word "external-repo" -- matches the documented and
        # templated format (spec-final.md, STATUS.yaml.template).
        if isinstance(dst, str) and dst not in by_id and not _EXTERNAL_EDGE_RE.match(dst):
            issues.append(Issue("fail", "-", f"edge.to references unknown claim {dst!r}"))
        if not isinstance(probe, str) or not probe:
            issues.append(Issue("fail", "-", f"edge {src!r}->{dst!r} missing probe"))
        else:
            full = root / probe
            if not full.is_file():
                issues.append(Issue("fail", "-", f"edge probe {probe!r} does not exist"))
            elif not os.access(full, os.X_OK):
                issues.append(Issue("fail", "-", f"edge probe {probe!r} is not executable"))

    seen_debt_ids: set[str] = set()
    for entry in data.get("debt") or []:
        if not isinstance(entry, dict):
            continue
        did = entry.get("id", "-")
        if did in seen_debt_ids:
            issues.append(Issue("fail", did, "duplicate debt id"))
        seen_debt_ids.add(did)
        if entry.get("severity") not in _DEBT_SEVERITY:
            issues.append(
                Issue(
                    "fail",
                    did,
                    f"debt.severity must be one of {sorted(_DEBT_SEVERITY)}, got {entry.get('severity')!r}",
                )
            )
        if entry.get("state") not in _DEBT_STATE:
            issues.append(
                Issue(
                    "fail",
                    did,
                    f"debt.state must be one of {sorted(_DEBT_STATE)}, got {entry.get('state')!r}",
                )
            )
        ref = entry.get("ref")
        if ref not in by_id:
            issues.append(Issue("fail", did, f"debt.ref references unknown claim {ref!r}"))
            continue
        ref_claim = by_id[ref]
        if entry.get("state") == "resolved" and ref_claim.get("status") in (
            "stub",
            "partial",
            "absent",
        ):
            issues.append(
                Issue(
                    "fail",
                    did,
                    f"debt marked resolved while claim {ref!r} is still {ref_claim.get('status')}",
                )
            )
        elif (
            entry.get("state") in ("open", "accepted")
            and ref_claim.get("status") == "implemented"
            and entry.get("severity") == "blocking"
        ):
            # normal/cosmetic debt left open on an implemented claim is routine cleanup, not a signal
            issues.append(
                Issue(
                    "warn",
                    did,
                    f"debt is {entry.get('state')} but claim {ref!r} is already implemented "
                    "(blocking debt on an implemented claim) -- close the debt or explain why "
                    "it is still open",
                )
            )

    if mode == "full":
        pattern = re.compile(r"STATUS\.yaml#([A-Za-z0-9_]+)")
        try:
            tracked = _git_ls_files(root)
        except GitUnavailable as exc:
            issues.append(
                Issue(
                    "fail",
                    "-",
                    f"could not enumerate tracked files, STATUS.yaml# cross-reference scan did not run: {exc}",
                )
            )
            tracked = []
        for rel in tracked:
            if not rel.endswith(".md") or _under_human_docs(rel):
                continue
            full = root / rel
            if not full.is_file():
                continue
            try:
                text = full.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for m in pattern.finditer(text):
                ref_id = m.group(1)
                if ref_id not in by_id:
                    issues.append(
                        Issue("fail", "-", f"{rel}: reference to unknown claim STATUS.yaml#{ref_id}")
                    )

    return issues


def _count_assertions(root: Path, nodeid: str) -> int | None:
    path_part, _, qual = nodeid.partition("::")
    if not qual:
        return None
    full = _within_root(root, path_part)
    if full is None:
        return None
    if not full.is_file():
        return None
    try:
        tree = ast.parse(full.read_text(), filename=str(full))
    except SyntaxError:
        return None
    scope: list[ast.stmt] = tree.body
    node = None
    for part in qual.split("::"):
        node = None
        for candidate in scope:
            if (
                isinstance(candidate, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                and candidate.name == part
            ):
                node = candidate
                break
        if node is None:
            return None
        scope = node.body
    if node is None:
        return None
    count = 0
    for sub in ast.walk(node):
        if isinstance(sub, ast.Assert):
            count += 1
        elif isinstance(sub, ast.Call):
            func = sub.func
            if isinstance(func, ast.Attribute) and func.attr.startswith("assert"):
                count += 1
            elif isinstance(func, ast.Name) and func.id != "assert" and func.id.startswith("assert"):
                count += 1
        elif isinstance(sub, ast.With):
            # `with pytest.raises(...)` / `with self.assertRaises(...)` proves
            # something just as much as a bare assert -- count it too, or every
            # stub-style test written the standard pytest way gets a permanent
            # false-positive "no assertions" warning.
            for item in sub.items:
                call = item.context_expr
                if not isinstance(call, ast.Call):
                    continue
                fn = call.func
                name = fn.attr if isinstance(fn, ast.Attribute) else fn.id if isinstance(fn, ast.Name) else None
                if name in ("raises", "warns", "assertRaises", "assertWarns"):
                    count += 1
    return count


def check_assertion_density(root: Path, data: dict, mode: str = "sync") -> list[Issue]:
    """A pytest check with zero assertions cannot fail, so it proves nothing.

    On a claim that already existed in the baseline this is a WARN: the check
    is weak, but the claim is not making a new promise today. On a claim
    written in this very change it is a FAIL -- an author who adds a claim and
    points it at an empty test has produced a green contract line for free,
    and that is exactly the shape the contract exists to stop.
    """
    baseline, _ = _baseline_claims(root, mode)
    # The warn Issue is deliberately dropped here: check_status_raise reports
    # an unusable baseline once, and the same line twice helps nobody. With no
    # baseline every claim reads as pre-existing, so nothing is raised to FAIL.
    issues: list[Issue] = []
    skipped_kinds: dict[str, int] = {}
    for claim in data.get("claims") or []:
        if not isinstance(claim, dict) or claim.get("kind") != "status":
            continue
        if claim.get("check_kind") != "pytest":
            kind = claim.get("check_kind")
            if isinstance(kind, str) and kind:
                skipped_kinds[kind] = skipped_kinds.get(kind, 0) + 1
            continue
        cid = claim.get("id", "-")
        check = claim.get("check")
        if not isinstance(check, str):
            continue
        for part in check.split(";"):
            part = part.strip()
            if not part:
                continue
            count = _count_assertions(root, part)
            if count == 0:
                is_new = baseline is not None and cid not in baseline
                if is_new:
                    issues.append(
                        Issue(
                            "fail",
                            cid,
                            f"{part}: test has no assertions -- a new claim must ship a "
                            "check that can fail",
                        )
                    )
                else:
                    issues.append(Issue("warn", cid, f"{part}: test has no assertions"))
    if skipped_kinds:
        issues.append(
            Issue(
                "warn",
                "-",
                "assertion density checked only for pytest claims; "
                f"{sum(skipped_kinds.values())} claims skipped "
                f"(kinds: {', '.join(sorted(skipped_kinds))})",
            )
        )
    return issues


def check_debt_coverage(root: Path, data: dict) -> list[Issue]:
    """Every stub/partial/absent claim needs an open or accepted debt entry.

    Without it the honest half of the contract costs nothing: a claim can sit
    at stub for a year with no record that somebody still owes the work, and
    the gap reads as a deliberate decision rather than an unpaid bill.
    design-only is exempt -- it is a decision, not a debt.
    """
    open_refs = {
        entry.get("ref")
        for entry in data.get("debt") or []
        if isinstance(entry, dict) and entry.get("state") in ("open", "accepted")
    }
    issues: list[Issue] = []
    for claim in data.get("claims") or []:
        if not isinstance(claim, dict) or claim.get("kind") != "status":
            continue
        status = claim.get("status")
        if status not in ("stub", "partial", "absent"):
            continue
        cid = claim.get("id", "-")
        if cid not in open_refs:
            issues.append(
                Issue(
                    "fail",
                    cid,
                    f"status {status} without open/accepted debt -- add a debt entry "
                    f"with ref: {cid}",
                )
            )
    return issues


def _baseline_debt_states(toplevel: Path, root: Path, base: str) -> dict[str, str]:
    """debt id -> state from the baseline STATUS.yaml. Empty when it does not
    parse -- the caller only reaches here after _baseline_claims already
    parsed the same document for the same base, so that is not expected to
    happen in practice."""
    shown = _git_run(toplevel, ["show", f"{base}:{_rel_status_path(toplevel, root)}"])
    if shown.returncode != 0:
        return {}
    try:
        doc = yaml.safe_load(shown.stdout)
    except yaml.YAMLError:
        return {}
    if not isinstance(doc, dict):
        return {}
    out: dict[str, str] = {}
    for entry in doc.get("debt") or []:
        if isinstance(entry, dict) and isinstance(entry.get("id"), str):
            out[entry["id"]] = entry.get("state")
    return out


def _debt_resolve_issues(root: Path, data: dict, mode: str) -> list[Issue]:
    baseline_claims, warn = _baseline_claims(root, mode)
    if baseline_claims is None:
        return [warn] if warn is not None else []
    toplevel = _git_toplevel(root)
    base, label, _ = _baseline_ref(toplevel, root, mode)
    if base is None:
        return []
    baseline_debt = _baseline_debt_states(toplevel, root, base)
    changed = _changed_paths(toplevel, root, base, mode)

    by_id = {
        c["id"]: c
        for c in data.get("claims") or []
        if isinstance(c, dict) and isinstance(c.get("id"), str)
    }
    issues: list[Issue] = []
    for entry in data.get("debt") or []:
        if not isinstance(entry, dict) or entry.get("state") != "resolved":
            continue
        did = entry.get("id")
        if not isinstance(did, str):
            continue
        if baseline_debt.get(did) not in ("open", "accepted", None):
            # already resolved at the baseline too -- not a fresh close
            continue
        ref_claim = by_id.get(entry.get("ref"))
        if ref_claim is None:
            continue
        old_claim = baseline_claims.get(entry.get("ref"))
        if old_claim is not None:
            if ref_claim.get("check") != old_claim.get("check"):
                continue
            if ref_claim.get("check_kind") != old_claim.get("check_kind"):
                continue
        check_kind = ref_claim.get("check_kind")
        targets = _check_targets(ref_claim) or _claim_path_targets(ref_claim)
        if not targets:
            issues.append(
                Issue(
                    "warn",
                    did,
                    f"debt {did} resolved without proof: claim {entry.get('ref')} has no "
                    "check or path to verify against; fix: name the commit or test in "
                    "description",
                )
            )
            continue
        if any(_target_changed(t, check_kind, changed) for t in targets):
            continue
        opened = entry.get("opened")
        if isinstance(opened, str) and _DATE_RE.match(opened):
            pathspecs = _debt_target_pathspecs(targets, check_kind, toplevel, root)
            if pathspecs:
                touched = _git_run(
                    toplevel,
                    ["log", "-1", "--format=%h %as", f"--since={opened}", "--", *pathspecs],
                )
                if touched.returncode == 0 and touched.stdout.strip():
                    sha, _, date = touched.stdout.strip().partition(" ")
                    issues.append(
                        Issue(
                            "warn",
                            did,
                            f"debt {did} resolved without a change in this range; targets "
                            f"last touched {sha} {date}, after the debt was opened; fix: "
                            "name that commit or test in description",
                        )
                    )
                    continue
        issues.append(
            Issue(
                "fail",
                did,
                f"debt {did} resolved without proof: claim {entry.get('ref')} check "
                f"unchanged and targets untouched since {label}; fix: change the "
                "check/test that proves it, or keep state: open",
            )
        )
    return issues


def check_debt_resolve(root: Path, data: dict, mode: str) -> list[Issue]:
    """FAIL when a debt entry closes without leaving proof in code or tests.

    A debt whose baseline state was open/accepted -- or that the baseline did
    not have at all, which is just as suspicious -- and is resolved now is
    only honest when the claim it backs shows work: its check or check_kind
    text changed since the baseline, or one of its target files did. Neither
    happening means the debt was closed on paper alone. FAIL in both modes,
    same as check_status_raise: closing a debt without proof is exactly the
    kind of dishonest edit the structural checks cannot otherwise see.

    Before that FAIL, a debt with a dated `opened` gets one more look: if a
    commit since then touched the claim's targets -- just not in this
    base..worktree range, e.g. the fix landed in an earlier, already-merged
    change -- this is WARN instead, in both modes, naming that commit.
    """
    try:
        return _debt_resolve_issues(root, data, mode)
    except GitUnavailable as exc:
        severity = "fail" if mode == "full" else "warn"
        return [Issue(severity, "-", f"could not read baseline STATUS.yaml: {exc}")]


def check_canary_roots(root: Path, data: dict, mode: str) -> list[Issue]:
    """One mutation canary per coverage root, not one per repository.

    A single canary in a repo with five roots proves the checks under one root
    have teeth and says nothing about the other four. The roots are the
    boundary the contract already draws, so each one carries its own proof. A
    root whose claims are all stub/absent/design-only needs none: there is no
    implementation there for a mutation to break.
    """
    meta = data.get("meta") or {}
    coverage_cfg = meta.get("coverage") if isinstance(meta, dict) else None
    roots = coverage_cfg.get("roots") if isinstance(coverage_cfg, dict) else None
    claims = [c for c in data.get("claims") or [] if isinstance(c, dict) and c.get("kind") == "status"]

    issues: list[Issue] = []
    for r in roots or []:
        prefix = _root_prefix(r)
        if prefix is None:
            continue
        proven = 0
        has_canary = False
        for claim in claims:
            raw = claim.get("path")
            if raw is None:
                continue
            paths = [raw] if isinstance(raw, str) else raw
            if not isinstance(paths, list):
                continue
            under = False
            for entry in paths:
                if not isinstance(entry, str) or not entry:
                    continue
                rel = Path(os.path.normpath(entry)).as_posix()
                if _path_in_root(rel, prefix):
                    under = True
                    break
            if not under:
                continue
            if claim.get("canary") and claim.get("status") in ("implemented", "partial"):
                has_canary = True
            if claim.get("status") in ("implemented", "partial") and claim.get("check_kind") in _CHECK_KINDS:
                proven += 1
        if proven and not has_canary:
            severity = "fail" if mode == "full" else "warn"
            issues.append(
                Issue(
                    severity,
                    "-",
                    f"coverage root {r}: {proven} implemented/partial claims, no canary "
                    "-- add canary: true + mutation to one of them",
                )
            )
    return issues


def check_audit_due(root: Path, data: dict, mode: str) -> list[Issue]:
    """Schedule a review without treating calendar age as a failed behavior check.

    Two proxies for the same thing -- the repository has moved far enough that
    nobody has checked the claims against what is actually there. Calendar age
    catches a contract nobody has opened in a month; the file count catches a
    month of heavy work compressed into a week. A missing last_audited_commit
    is only ever a WARN: repositories audited before the field existed must
    keep passing.
    """
    meta = data.get("meta")
    if not isinstance(meta, dict):
        return []
    issues: list[Issue] = []
    reasons: list[str] = []

    raw_date = str(meta.get("last_audited", ""))
    if _DATE_RE.match(raw_date):
        try:
            audited = datetime.date.fromisoformat(raw_date)
        except ValueError:
            audited = None
        if audited is not None:
            age = (datetime.date.today() - audited).days
            if age > _AUDIT_MAX_DAYS:
                reasons.append(
                    f"last audited {raw_date} ({age}d ago, limit {_AUDIT_MAX_DAYS}d)"
                )

    commit = meta.get("last_audited_commit")
    if not isinstance(commit, str) or not commit:
        issues.append(
            Issue(
                "warn",
                "-",
                "meta.last_audited_commit missing; the audit close step writes it",
            )
        )
    else:
        try:
            toplevel = _git_toplevel(root)
            resolved = _git_run(toplevel, ["rev-parse", "--verify", f"{commit}^{{commit}}"])
            if resolved.returncode != 0:
                issues.append(
                    Issue(
                        "warn",
                        "-",
                        f"meta.last_audited_commit {commit!r} does not resolve to a commit",
                    )
                )
            else:
                coverage_cfg = meta.get("coverage")
                roots = coverage_cfg.get("roots") or [] if isinstance(coverage_cfg, dict) else []
                excludes = coverage_cfg.get("exclude") or [] if isinstance(coverage_cfg, dict) else []
                paths = _rebase_to_root(_diff_paths(toplevel, [commit, "HEAD"]), toplevel, root)
                changed = sum(1 for rel in paths if _under_coverage_roots(rel, roots, excludes))
                if changed > _AUDIT_MAX_FILES:
                    reasons.append(
                        f"{changed} files changed under the coverage roots since "
                        f"{commit} (limit {_AUDIT_MAX_FILES})"
                    )
        except GitUnavailable as exc:
            issues.append(Issue("warn", "-", f"audit-due check skipped: {exc}"))

    if reasons:
        issues.append(
            Issue(
                "warn",
                "-",
                "audit due: " + "; ".join(reasons) + " -- run the ground-truth audit",
            )
        )
    return issues


def _allowlisted(match_text: str, line: str, allowed: set[str]) -> bool:
    for value in allowed:
        if value and match_text in value and value in line:
            return True
    return False


def check_no_banned_words(root: Path, data: dict) -> list[Issue]:
    issues: list[Issue] = []
    allowed: set[str] = set()
    meta = data.get("meta", {}) or {}
    # A product or its docs can have a legitimate reason to name these words;
    # such a repository turns the scan off explicitly, and every full run
    # says so rather than going quiet.
    banned_words_cfg = meta.get("banned_words")
    if isinstance(banned_words_cfg, dict) and banned_words_cfg.get("enabled") is False:
        return [Issue("warn", "-", "banned-words scan disabled by meta.banned_words.enabled")]
    if isinstance(meta.get("repo"), str):
        allowed.add(meta["repo"])
    for claim in data.get("claims") or []:
        if not isinstance(claim, dict):
            continue
        if isinstance(claim.get("id"), str):
            allowed.add(claim["id"])
        if isinstance(claim.get("component"), str):
            allowed.add(claim["component"])

    # check_shape is the source of the FAIL for a malformed or contract-artifact-
    # covering entry; here we only ever act on entries that are shape-valid and
    # do not cover contract artifacts. That second filter is deliberately
    # re-checked here (not just left to check_shape) -- the exclusion is never
    # honored for STATUS.yaml / tools/ground_truth/** / tests/**, no matter how
    # it got past shape validation.
    exclude_entries: list[tuple[str, str]] = []
    if isinstance(banned_words_cfg, dict):
        exclude_cfg = banned_words_cfg.get("exclude")
        if isinstance(exclude_cfg, list):
            for entry in exclude_cfg:
                if not isinstance(entry, dict):
                    continue
                path = entry.get("path")
                why = entry.get("why")
                if not isinstance(path, str) or not path:
                    continue
                if not isinstance(why, str) or not why:
                    continue
                if _covers_contract_artifacts(path):
                    continue
                exclude_entries.append((path, why))

    try:
        tracked = _git_ls_files(root, include_untracked=True)
    except GitUnavailable as exc:
        # A scan that could not run must not be reported as a scan that
        # found nothing -- this is the mandatory D16/SC-g gate, not optional.
        return [
            Issue(
                "fail",
                "-",
                f"could not enumerate tracked files, banned-word scan did not run: {exc}",
            )
        ]

    exclude_hits = [0] * len(exclude_entries)
    for rel in tracked:
        if _under_human_docs(rel):
            continue
        excluded = False
        for i, (pat, _why) in enumerate(exclude_entries):
            if fnmatch.fnmatch(rel, pat):
                exclude_hits[i] += 1
                excluded = True
        if excluded:
            continue
        full = root / rel
        if not full.is_file():
            continue
        try:
            raw = full.read_bytes()
        except OSError:
            continue
        if len(raw) > 2 * 1024 * 1024:
            continue
        if b"\x00" in raw[:8192]:
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if "gt-allow:" in line:
                continue
            for pat in _FAIL_PATTERNS:
                m = pat.search(line)
                if m and not _allowlisted(m.group(0), line, allowed):
                    issues.append(Issue("fail", "-", f"{rel}:{lineno}: banned word {m.group(0)!r}"))
            for pat in _WARN_PATTERNS:
                m = pat.search(line)
                if m and not _allowlisted(m.group(0), line, allowed):
                    issues.append(Issue("warn", "-", f"{rel}:{lineno}: buzzword {m.group(0)!r}"))

    for (pat, why), hits in zip(exclude_entries, exclude_hits):
        if hits:
            issues.append(
                Issue(
                    "warn",
                    "-",
                    f"banned-word scan skipped {hits} file(s) matching {pat!r} ({why})",
                )
            )
        else:
            issues.append(
                Issue("warn", "-", f"banned-word exclude {pat!r} matched no tracked file")
            )
    return issues


def _find_claim_block(lines: list[str], claim_id: str) -> tuple[int, int] | None:
    needle = f"- id: {claim_id}"
    start = None
    for i, line in enumerate(lines):
        if line.strip() == needle:
            start = i
            break
    if start is None:
        return None
    end = len(lines)
    for j in range(start + 1, len(lines)):
        stripped = lines[j].strip()
        if stripped.startswith("- id:"):
            end = j
            break
        if lines[j] and not lines[j][0].isspace() and ":" in lines[j]:
            end = j
            break
    return start + 1, end


def check_stale_live_state(root: Path, data: dict) -> list[Issue]:
    issues: list[Issue] = []
    meta = data.get("meta", {}) or {}
    stale_after_days = meta.get("stale_after_days", _STALE_DEFAULT_DAYS)
    status_path = root / "STATUS.yaml"
    if not status_path.is_file():
        return issues
    lines = status_path.read_text().splitlines()

    for claim in data.get("claims") or []:
        if not isinstance(claim, dict) or claim.get("kind") != "live_state":
            continue
        cid = claim.get("id")
        if not isinstance(cid, str):
            continue
        block = _find_claim_block(lines, cid)
        if block is None:
            continue
        start_line, end_line = block
        try:
            result = subprocess.run(
                [
                    "git",
                    "log",
                    "-1",
                    "--format=%ad",
                    "--date=short",
                    f"-L{start_line},{end_line}:STATUS.yaml",
                ],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except OSError:
            continue
        out = result.stdout.strip()
        if not out:
            continue
        date_str = out.splitlines()[0].strip()
        if not _DATE_RE.match(date_str):
            continue
        try:
            audited = datetime.date.fromisoformat(date_str)
        except ValueError:
            continue
        age = (datetime.date.today() - audited).days
        if age > stale_after_days:
            issues.append(
                Issue(
                    "warn",
                    cid,
                    f"live_state block last touched {date_str} ({age}d ago, limit {stale_after_days}d)",
                )
            )
    return issues


_STATUS_RANK = {
    "absent": 0,
    "design-only": 0,
    "stub": 1,
    "partial": 2,
    "implemented": 3,
}


def _git_run(root: Path, args: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
    """Run one git command. Only an unrunnable git raises here -- a nonzero
    exit is data for the caller (git show on a path the baseline lacks,
    rev-parse on an unresolvable ref), not always a broken environment."""
    try:
        return subprocess.run(
            ["git", *args], cwd=root, capture_output=True, text=True, timeout=timeout
        )
    except OSError as exc:
        raise GitUnavailable(f"could not run git {' '.join(args)}: {exc}") from exc


def _git_toplevel(root: Path) -> Path:
    # Works in a linked worktree too, where .git is a file, not a directory.
    cp = _git_run(root, ["rev-parse", "--show-toplevel"])
    if cp.returncode != 0 or not cp.stdout.strip():
        raise GitUnavailable(
            f"git rev-parse --show-toplevel failed (exit {cp.returncode}): {cp.stderr.strip()}"
        )
    return Path(cp.stdout.strip())


def _porcelain_paths(toplevel: Path) -> list[str]:
    """Every path git status reports: all statuses including untracked, and
    both sides of a rename or copy."""
    cp = _git_run(toplevel, ["status", "--porcelain=v1", "-z"])
    if cp.returncode != 0:
        raise GitUnavailable(
            f"git status --porcelain failed (exit {cp.returncode}): {cp.stderr.strip()}"
        )
    fields = cp.stdout.split("\0")
    paths: list[str] = []
    i = 0
    while i < len(fields):
        entry = fields[i]
        i += 1
        if len(entry) < 4:
            continue
        xy, path = entry[:2], entry[3:]
        paths.append(path)
        if ("R" in xy or "C" in xy) and i < len(fields):
            # In -z output the rename pair is reversed and the origin path
            # arrives as its own NUL-terminated field right after the entry.
            origin = fields[i]
            i += 1
            if origin:
                paths.append(origin)
    return paths


def _diff_paths(toplevel: Path, args: list[str]) -> list[str]:
    cp = _git_run(toplevel, ["diff", "--name-only", "-z", *args])
    if cp.returncode != 0:
        raise GitUnavailable(
            f"git diff --name-only {' '.join(args)} failed (exit {cp.returncode}): "
            f"{cp.stderr.strip()}"
        )
    return [p for p in cp.stdout.split("\0") if p]


def _numstat_paths(toplevel: Path, args: list[str]) -> tuple[set[str], set[str]]:
    """Split one diff into (paths with no content change, paths with content
    change). git reports a path as changed for a pure mode change (chmod) or
    an identical-content rename too; such an entry must not count as touching
    a check target, because the bytes the check reads did not move.

    Unparseable entries (a quoted path with special characters, a rename pair)
    are reported as content changes: the conservative side is to leave the
    path in the changed set rather than drop it on a guess."""
    cp = _git_run(toplevel, ["diff", "--numstat", *args])
    if cp.returncode != 0:
        return set(), set()
    empty: set[str] = set()
    content: set[str] = set()
    for line in cp.stdout.splitlines():
        fields = line.split("\t")
        if len(fields) < 3:
            continue
        added, deleted, path = fields[0], fields[1], "\t".join(fields[2:])
        if path.startswith('"') or " => " in path:
            content.add(path)
            continue
        if added == "0" and deleted == "0":
            empty.add(path)
        else:
            content.add(path)
    return empty, content


def _rebase_to_root(paths: list[str], toplevel: Path, root: Path) -> set[str]:
    """git reports paths relative to the toplevel; claims are relative to
    root. Paths outside root are dropped -- they cannot be a check target."""
    top_resolved = toplevel.resolve()
    root_resolved = root.resolve()
    out: set[str] = set()
    for p in paths:
        if not p:
            continue
        full = (top_resolved / os.path.normpath(p)).resolve()
        try:
            out.add(full.relative_to(root_resolved).as_posix())
        except ValueError:
            continue
    return out


def _check_targets(claim: dict) -> list[str]:
    """Files (for go_test the package directory, for junit the test class's dotted fqn) a claim's check reads."""
    check = claim.get("check")
    if not isinstance(check, str):
        return []
    targets: list[str] = []
    for part in check.split(";"):
        part = part.strip()
        if not part:
            continue
        head = part.split("::", 1)[0].strip()
        if not head:
            continue
        targets.append(Path(os.path.normpath(head)).as_posix())
    return targets


def _claim_path_targets(claim: dict) -> list[str]:
    """Fallback targets for a ref claim with no check: its path field(s)."""
    raw = claim.get("path")
    paths = [raw] if isinstance(raw, str) else raw if isinstance(raw, list) else []
    return [Path(os.path.normpath(p)).as_posix() for p in paths if isinstance(p, str) and p]


def _target_changed(target: str, check_kind: str, changed: set[str]) -> bool:
    if check_kind == "go_test":
        prefix = target.rstrip("/") + "/"
        return any(c.startswith(prefix) and c.endswith("_test.go") for c in changed)
    if check_kind == "junit":
        suffix = "src/test/java/" + target.replace(".", "/") + ".java"
        return any(c.endswith(suffix) for c in changed)
    return target in changed


def _debt_target_pathspecs(
    targets: list[str], check_kind: str, toplevel: Path, root: Path
) -> list[str]:
    """git pathspecs (relative to toplevel) for a debt's proof-of-work check.
    Same per-kind shape as _target_changed: a directory for go_test, the
    derived test-class file for junit, the literal path otherwise."""
    specs: list[str] = []
    for target in targets:
        if check_kind == "go_test":
            rel = target.rstrip("/") + "/"
        elif check_kind == "junit":
            rel = "src/test/java/" + target.replace(".", "/") + ".java"
        else:
            rel = target
        try:
            specs.append((root / rel).resolve().relative_to(toplevel.resolve()).as_posix())
        except ValueError:
            continue
    return specs


def _baseline_status_claims(text: str) -> dict[str, dict] | None:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return None
    if not isinstance(data, dict):
        return None
    claims = data.get("claims")
    if "claims" in data and not isinstance(claims, list):
        # claims: null, a string, a mapping -- parseable YAML, unusable as a
        # baseline. Treat it like a broken baseline (WARN), never as "the
        # baseline had no claims", which would wave every raise through.
        return None
    out: dict[str, dict] = {}
    for claim in claims or []:
        if not isinstance(claim, dict) or claim.get("kind") != "status":
            continue
        cid = claim.get("id")
        if isinstance(cid, str):
            out[cid] = claim
    return out


def _rel_status_path(toplevel: Path, root: Path) -> str | None:
    """STATUS.yaml as git names it, or None when it sits outside this
    repository and there is nothing to compare it against."""
    status_path = (root / "STATUS.yaml").resolve()
    try:
        return status_path.relative_to(toplevel.resolve()).as_posix()
    except ValueError:
        return None


def _baseline_ref(toplevel: Path, root: Path, mode: str) -> tuple[str | None, str, Issue | None]:
    """Which commit the current STATUS.yaml is read against: (base, label,
    warn). base is None when there is nothing to compare with -- silently so
    while a repository is still being initialized, with a WARN when a base ref
    was named and did not resolve."""
    rel_status = _rel_status_path(toplevel, root)
    if rel_status is None:
        return None, "-", None
    if mode == "sync":
        tracked = _git_run(toplevel, ["ls-files", "--error-unmatch", "--", rel_status])
        if tracked.returncode != 0:
            # Untracked STATUS.yaml: init is still in progress, stay silent.
            return None, "HEAD", None
        return "HEAD", "HEAD", None
    env_ref = os.environ.get("GT_BASE_REF", "").strip()
    # An explicit GT_BASE_REF that does not resolve (all-zero sha from a
    # first push to a branch, a deleted ref, a shallow clone) is never
    # replaced by HEAD~1: comparing against an arbitrary commit would give
    # an arbitrary verdict. The fallback exists only when CI set nothing.
    candidate = env_ref or "HEAD~1"
    cp = _git_run(toplevel, ["rev-parse", "--verify", f"{candidate}^{{commit}}"])
    if cp.returncode == 0:
        return candidate, candidate, None
    if env_ref:
        detail = f"GT_BASE_REF '{env_ref}' does not resolve to a commit"
    else:
        detail = "no base ref (set GT_BASE_REF)"
    return None, candidate, Issue("warn", "-", f"status-raise guard skipped: {detail}")


def _baseline_claims(root: Path, mode: str) -> tuple[dict[str, dict] | None, Issue | None]:
    """kind: status claims of the baseline STATUS.yaml, keyed by id, plus the
    one Issue to report when there is no usable baseline. Two checks need this
    same view -- the raise guard to compare statuses, assertion density to
    tell a new claim from an old one -- and they must not disagree about which
    commit "before this change" means."""
    try:
        toplevel = _git_toplevel(root)
        base, label, warn = _baseline_ref(toplevel, root, mode)
        if base is None:
            return None, warn
        shown = _git_run(toplevel, ["show", f"{base}:{_rel_status_path(toplevel, root)}"])
        if shown.returncode != 0:
            # The baseline has no STATUS.yaml at that path: first contract commit.
            return None, None
        claims = _baseline_status_claims(shown.stdout)
        if claims is None:
            return None, Issue(
                "warn",
                "-",
                f"status-raise guard skipped: baseline STATUS.yaml at {label} does not parse",
            )
        return claims, None
    except GitUnavailable as exc:
        severity = "fail" if mode == "full" else "warn"
        return None, Issue(severity, "-", f"could not read baseline STATUS.yaml: {exc}")


def _claim_path_changed(claim: dict, changed: set[str]) -> bool:
    """True when a file the claim covers is in the change-set. A path entry
    naming a directory counts when any file below it changed."""
    raw = claim.get("path")
    if raw is None:
        return False
    paths = [raw] if isinstance(raw, str) else raw
    if not isinstance(paths, list):
        return False
    for p in paths:
        if not isinstance(p, str) or not p:
            continue
        rel = Path(os.path.normpath(p)).as_posix()
        prefix = rel.rstrip("/") + "/"
        if any(c == rel or c.startswith(prefix) for c in changed):
            return True
    return False


def _changed_paths(toplevel: Path, root: Path, base: str, mode: str) -> set[str]:
    """Repo-relative paths changed since base: a dirty working tree counts in
    both modes (the change under review may not be committed yet when the
    Stop hook or a local full run looks at it), plus the committed diff in
    full mode. Shared by check_status_raise and check_debt_resolve -- both
    need "did this file's content move since the baseline" and must not
    disagree about what "moved" means."""
    raw_changed = _porcelain_paths(toplevel)
    diff_arg_sets = [["HEAD"]] if mode == "sync" else [[base, "HEAD"], ["HEAD"]]
    raw_changed += _diff_paths(toplevel, diff_arg_sets[0])
    empty: set[str] = set()
    content: set[str] = set()
    for diff_args in diff_arg_sets:
        e, c = _numstat_paths(toplevel, diff_args)
        empty |= e
        content |= c
    # A path that is content-free in every diff consulted (chmod only) does
    # not count as touched. Untracked paths never appear in numstat, so they
    # stay in the set.
    drop = empty - content
    return _rebase_to_root([p for p in raw_changed if p not in drop], toplevel, root)


def _status_raise_issues(root: Path, data: dict, mode: str) -> list[Issue]:
    baseline, warn = _baseline_claims(root, mode)
    if baseline is None:
        return [warn] if warn is not None else []
    toplevel = _git_toplevel(root)
    base, label, _ = _baseline_ref(toplevel, root, mode)
    if base is None:
        return []
    changed = _changed_paths(toplevel, root, base, mode)

    issues: list[Issue] = []
    for claim in data.get("claims") or []:
        if not isinstance(claim, dict) or claim.get("kind") != "status":
            continue
        cid = claim.get("id")
        if not isinstance(cid, str):
            continue
        old = baseline.get(cid)
        if old is None:
            # A claim that did not exist in the baseline cannot be a raise.
            continue
        old_rank = _STATUS_RANK.get(old.get("status"))
        new_rank = _STATUS_RANK.get(claim.get("status"))
        if old_rank is None or new_rank is None or new_rank <= old_rank:
            continue
        if claim.get("check") != old.get("check"):
            continue
        if claim.get("check_kind") != old.get("check_kind"):
            continue
        check_kind = claim.get("check_kind")
        targets = _check_targets(claim)
        if not targets:
            continue
        if any(_target_changed(t, check_kind, changed) for t in targets):
            continue
        if _claim_path_changed(claim, changed):
            # The code the claim covers moved in this same change-set, so the
            # raise is about new behaviour rather than a lone edit of
            # STATUS.yaml. The check still has to be green for the new status
            # to stand -- full mode proves that here, sync leaves it to the
            # Stop hook enforcer, which runs the check on every turn.
            if mode == "full":
                ran_ok, msg, skipped = run_claim_check(root, claim)
                if skipped:
                    issues.append(
                        Issue("warn", cid, f"status raised, check not run: {msg}")
                    )
                elif not ran_ok:
                    issues.append(
                        Issue("fail", cid, f"status raised, check untouched and red: {msg}")
                    )
            continue
        issues.append(
            Issue(
                "fail",
                cid,
                f"status raised {old.get('status')} -> {claim.get('status')} but check "
                f"target(s) {', '.join(targets)} unchanged since {label}; strengthen the "
                "check in the same change",
            )
        )
    return issues


def check_status_raise(root: Path, data: dict, mode: str) -> list[Issue]:
    """FAIL when a claim's status is raised without touching what proves it.

    Rank order: absent/design-only < stub < partial < implemented. A claim
    whose rank goes up while its check text, its check_kind and every file
    the check reads all stay untouched since the baseline is the one
    dishonest edit the structural checks cannot see -- the form stays valid
    and the check still resolves, so nothing else here objects.
    """
    try:
        return _status_raise_issues(root, data, mode)
    except GitUnavailable as exc:
        severity = "fail" if mode == "full" else "warn"
        return [Issue(severity, "-", f"could not read baseline STATUS.yaml: {exc}")]


def check_cited_lines(root: Path, data: dict, mode: str) -> list[Issue]:
    """WARN (sync) / FAIL (full) when a note's file:line citation and its
    quoted text disagree.

    Only a citation with a quote right after it is checked -- a bare
    path:N with no quote is remap_line_refs.py's job (it shifts the number
    when the file changes), not this check's. A missing file is skipped
    too: check_coverage/check_shape already own path existence.
    """
    severity = "warn" if mode == "sync" else "fail"
    issues: list[Issue] = []
    for claim in data.get("claims") or []:
        if not isinstance(claim, dict):
            continue
        note = claim.get("note")
        if not isinstance(note, str):
            continue
        cid = claim.get("id", "-")
        for m in _CITE_RE.finditer(note):
            window = note[m.end() : m.end() + 120].split(";", 1)[0]
            qm = _QUOTE_RE.search(window)
            if not qm:
                continue
            quote = next((g for g in qm.groups() if g is not None), "").strip()
            if not quote:
                continue
            path = m.group("path")
            line_no = int(m.group("line"))
            line_end_raw = m.group("line_end")
            line_end = int(line_end_raw) if line_end_raw and int(line_end_raw) >= line_no else None
            full = _within_root(root, path)
            if full is None or not full.is_file():
                continue
            try:
                lines = full.read_text(errors="replace").splitlines()
            except OSError:
                continue
            if line_end is not None:
                last = min(line_end, len(lines))
                if any(quote in lines[n - 1].strip() for n in range(line_no, last + 1)):
                    continue
                label = f"{path}:{line_no}-{line_end}"
            else:
                if 1 <= line_no <= len(lines) and quote in lines[line_no - 1].strip():
                    continue
                label = f"{path}:{line_no}"
            shown = quote if len(quote) <= 60 else quote[:60]
            issues.append(
                Issue(
                    severity,
                    cid,
                    f"cited line {label} does not contain «{shown}»; fix: run "
                    "tools/ground_truth/remap_line_refs.py --apply or update the note",
                )
            )
    return issues


def run(root: Path, mode: str) -> list[Issue]:
    data = load_contract(root)
    issues: list[Issue] = []
    issues += check_shape(root, data)
    issues += check_coverage(root, data, mode)
    issues += check_collectible(root, data, mode)
    issues += check_links(root, data, mode)
    issues += check_assertion_density(root, data, mode)
    issues += check_debt_coverage(root, data)
    issues += check_debt_resolve(root, data, mode)
    issues += check_canary_roots(root, data, mode)
    issues += check_audit_due(root, data, mode)
    issues += check_status_raise(root, data, mode)
    issues += check_cited_lines(root, data, mode)
    if mode == "full":
        issues += check_no_banned_words(root, data)
        issues += check_stale_live_state(root, data)
    return issues
