"""Run configuration and path helpers shared by every pipeline stage.

A run is described by a small run.json (see run.example.json): six string
fields and nothing else. Everything a stage needs beyond those six fields
(repo display name, coverage roots, runner, doc lists, model overrides,
seeds) is a CLI flag of that stage, never a run.json field -- this file
never references a repository name or a workflow/task id.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

_FIELDS = ("repo", "research", "scratch", "python", "skill", "date")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass(frozen=True)
class Run:
    repo: Path
    research: Path
    scratch: Path
    python: Path
    skill: Path
    date: str


def load_run(path: str | Path) -> Run:
    """Load and validate a run.json, creating run.scratch if it is missing.

    Raises ValueError if a field is missing/unexpected, if repo, research
    or skill do not exist, or if date does not match YYYY-MM-DD.
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))

    missing = [f for f in _FIELDS if f not in data]
    if missing:
        raise ValueError(f"run config missing fields: {missing}")
    extra = sorted(set(data) - set(_FIELDS))
    if extra:
        raise ValueError(f"run config has unexpected fields: {extra}")

    date = data["date"]
    if not isinstance(date, str) or not _DATE_RE.match(date):
        raise ValueError(f"run.date must match YYYY-MM-DD, got {date!r}")

    repo = Path(data["repo"])
    research = Path(data["research"])
    scratch = Path(data["scratch"])
    python = Path(data["python"])
    skill = Path(data["skill"])

    if not repo.is_dir():
        raise ValueError(f"run.repo does not exist: {repo}")
    if not research.is_dir():
        raise ValueError(f"run.research does not exist: {research}")
    if not skill.is_dir():
        raise ValueError(f"run.skill does not exist: {skill}")

    scratch.mkdir(parents=True, exist_ok=True)

    return Run(repo=repo, research=research, scratch=scratch, python=python, skill=skill, date=date)


def stage_dir(run: Run, phase: str) -> Path:
    """Return run.scratch/<phase>, creating it if needed."""
    d = run.scratch / phase
    d.mkdir(parents=True, exist_ok=True)
    return d


def research(run: Run, *parts: str) -> Path:
    """Join path parts under run.research."""
    return run.research.joinpath(*parts)


def args_path(run: Run, phase: str) -> Path:
    """Return the conventional args file for a phase: run.scratch/<phase>/<phase>-args.json."""
    return stage_dir(run, phase) / f"{phase}-args.json"


def rel(run: Run, p: str | Path) -> str:
    """Turn an absolute path under run.repo into a repo-relative posix-style path.

    Does not resolve symlinks and does not assume run.repo carries a
    trailing slash (a bare string-slice on an unmatched trailing slash
    silently produces an off-by-one path, which is exactly the bug this
    replaces every REPO_PREFIX constant in the old scratch scripts to
    avoid). A doubled separator right after run.repo (a common result of
    naive path joins in agent output) is also collapsed rather than left
    in the result. An already-relative input is returned unchanged.

    Raises ValueError for an absolute path that is not under run.repo --
    a finder can return a path outside the worktree, and silently turning
    it into something that merely looks repo-relative would let a claim
    cite a path that resolves to nothing.
    """
    s = str(p).strip()
    if not s:
        return s
    if not s.startswith("/"):
        return s
    repo_str = str(run.repo).rstrip("/")
    if s == repo_str:
        return ""
    if s.startswith(repo_str + "/"):
        return s[len(repo_str):].lstrip("/")
    raise ValueError(f"path is not under run.repo: {p!r}")
