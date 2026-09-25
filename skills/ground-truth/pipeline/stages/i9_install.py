#!/usr/bin/env python3
"""Install ground-truth verifier artifacts from run.skill/templates into run.repo.

--facts prints the install-relevant facts about run.repo as JSON and
exits (git-tracked .claude, CI file kind/path, pre-commit presence,
python/go/js runner detection, .venv/bin/python presence, CI cache paths
inside the checkout and which of them .gitignore misses) -- nothing
else runs in that mode.

Otherwise builds an install plan and either prints it (--dry-run, the
default) or applies it (--write). Every step compares source and
destination bytes first, so a second run against an already-installed
repo reports nothing changed (OK) rather than re-writing files or
duplicating CI/settings/pre-commit entries. Never runs a state-changing
git command.
"""
from __future__ import annotations

import argparse
import json
import re
import stat
import subprocess
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from gt_lib.git import ls_files  # noqa: E402
from gt_lib.paths import load_run  # noqa: E402

TOOLS_FILES = [
    "contract_lib.py",
    "verify.py",
    "gt_context.py",
    "blast_radius.py",
    "gt_mutation_selfcheck.py",
    "gt_session_guard.py",
    "gt_edit_guard.py",
    "gt_hook_state.py",
    "coldstart_budget.py",
    "remap_line_refs.py",
]
EXAMPLE_PROBE = "example_probe.py"
HOOK_FILES = [
    "ground-truth-sync-check.sh",
    "ground-truth-session-start.sh",
    "ground-truth-edit-guard.sh",
]
PY_BRIDGE = "status_contract_test.py.template"
GO_BRIDGE = "status_contract_test.go.template"
TS_BRIDGE = "status_contract.test.ts.template"
JAVA_BRIDGE = "status_contract_test.java.template"
RULE_FILE = "ground-truth.rule.md"

_INSTALLER_COMMENT_RE = re.compile(r"^<!--.*?-->\s*\n+", re.S)


# ---------------------------------------------------------------- facts --

_PYTHON_RUNNER_SKIP_DIRS = {".git", ".worktrees", ".venv", "node_modules", "__pycache__", ".mypy_cache", ".pytest_cache"}


def _iter_subdirs(root: Path, max_depth: int):
    """Yield directories under root (root itself excluded) up to max_depth,
    breadth-first, skipping vcs/worktree/vendor dirs."""
    frontier = [(root, 0)]
    while frontier:
        current, depth = frontier.pop(0)
        if depth >= max_depth:
            continue
        for child in sorted(p for p in current.iterdir() if p.is_dir() and p.name not in _PYTHON_RUNNER_SKIP_DIRS):
            yield child
            frontier.append((child, depth + 1))


def _has_python_files(d: Path) -> bool:
    return any(p.is_file() for p in d.rglob("*.py"))


def _find_nested_python_marker(repo: Path, require_py: bool = True) -> tuple[str, str] | None:
    """First pytest.ini / conftest.py / tests dir found up to depth 2 below repo.

    A tests dir alone says "python" only when it holds a *.py file: e2e/tests
    full of *.spec.ts is a JS suite. require_py=False is for placing the
    bridge test once python was already detected by a config file."""
    for d in _iter_subdirs(repo, max_depth=2):
        rel = d.relative_to(repo).as_posix()
        if d.name == "tests" and (not require_py or _has_python_files(d)):
            return "tests_dir", rel
        if (d / "pytest.ini").is_file():
            return "pytest.ini", rel
        if (d / "conftest.py").is_file():
            return "conftest.py", rel
    return None


def _resolve_tests_dir(repo: Path) -> str:
    if (repo / "tests").is_dir():
        return "tests"
    nested = _find_nested_python_marker(repo, require_py=False)
    return nested[1] if nested else "tests"


def _detect_python_runner(repo: Path) -> dict:
    if (repo / "pytest.ini").is_file():
        return {"detected": True, "via": "pytest.ini", "tests_dir": _resolve_tests_dir(repo)}
    pj = repo / "pyproject.toml"
    if pj.is_file() and "[tool.pytest.ini_options]" in pj.read_text(encoding="utf-8", errors="ignore"):
        return {"detected": True, "via": "pyproject.toml", "tests_dir": _resolve_tests_dir(repo)}
    sc = repo / "setup.cfg"
    if sc.is_file() and "[tool:pytest]" in sc.read_text(encoding="utf-8", errors="ignore"):
        return {"detected": True, "via": "setup.cfg", "tests_dir": _resolve_tests_dir(repo)}
    tx = repo / "tox.ini"
    if tx.is_file() and "[pytest]" in tx.read_text(encoding="utf-8", errors="ignore"):
        return {"detected": True, "via": "tox.ini", "tests_dir": _resolve_tests_dir(repo)}
    if (repo / "tests").is_dir() and _has_python_files(repo / "tests"):
        return {"detected": True, "via": "tests_dir", "tests_dir": "tests"}
    nested = _find_nested_python_marker(repo)
    if nested:
        via, tests_dir = nested
        return {"detected": True, "via": via, "tests_dir": tests_dir}
    return {"detected": False, "via": None, "tests_dir": None}


def _js_runner_of(pkg: Path) -> str | None:
    try:
        data = json.loads(pkg.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    deps = {}
    for key in ("dependencies", "devDependencies"):
        deps.update(data.get(key) or {})
    if "vitest" in deps:
        return "vitest"
    if "jest" in deps:
        return "jest"
    return None


def _detect_js_runner(repo: Path) -> dict:
    """package.json at the root or up to two levels below it (a monorepo keeps
    its jest config in backend/ or apps/web/); the root one wins, then the
    first nested one that names jest or vitest."""
    found = [repo / "package.json"] if (repo / "package.json").is_file() else []
    found += [d / "package.json" for d in _iter_subdirs(repo, max_depth=2) if (d / "package.json").is_file()]
    if not found:
        return {"present": False, "runner": None, "package_json": None}
    for pkg in found:
        runner = _js_runner_of(pkg)
        if runner:
            return {"present": True, "runner": runner, "package_json": pkg.relative_to(repo).as_posix()}
    return {"present": True, "runner": None, "package_json": None}


JS_NO_RUNNER_WARN = "WARN js present, runner not detected -> bridge skipped, use a probe"


def _detect_java_package(test_root: Path) -> str | None:
    """Shallowest src/test/java subdirectory holding a *.java file, as a
    dotted package name; ties broken by lexicographically first relative
    path (SKILL.md #5)."""
    candidates = {f.parent.relative_to(test_root) for f in test_root.rglob("*.java")}
    if not candidates:
        return None
    best = min(candidates, key=lambda p: (len(p.parts), p.as_posix()))
    if not best.parts:
        return None
    return best.as_posix().replace("/", ".")


def _detect_maven(repo: Path) -> dict:
    test_root_dir = repo / "src" / "test" / "java"
    test_root = "src/test/java" if test_root_dir.is_dir() else None
    return {
        "present": (repo / "pom.xml").is_file(),
        "test_root": test_root,
        "package": _detect_java_package(test_root_dir) if test_root else None,
    }


def _detect_ci(repo: Path, ci_file: str | None = None) -> dict:
    if ci_file:
        path = repo / ci_file
        if ci_file == ".gitlab-ci.yml" and not path.exists():
            # A GitLab host i9 cannot recognise by name: the job becomes the
            # whole file, same as for a detected gitlab origin.
            return {"kind": "gitlab", "path": ci_file, "create": True}
        if not path.is_file():
            raise FileNotFoundError(f"--ci-file {ci_file!r} does not exist in the repo")
        kind = "gitlab" if ci_file == ".gitlab-ci.yml" else "github"
        return {"kind": kind, "path": ci_file}
    gh_dir = repo / ".github" / "workflows"
    gh_files = []
    if gh_dir.is_dir():
        gh_files = sorted(gh_dir.glob("*.yml")) + sorted(gh_dir.glob("*.yaml"))
    # A workflow that already carries the job wins, so a second install
    # finds the job where the first one put it.
    for f in gh_files:
        if "verify-status-contract:" in f.read_text(encoding="utf-8", errors="ignore"):
            return {"kind": "github", "path": str(f.relative_to(repo))}
    if len(gh_files) == 1:
        return {"kind": "github", "path": str(gh_files[0].relative_to(repo))}
    if gh_files:
        # Several workflows and no --ci-file: picking one by name is a guess,
        # a workflow of its own is not.
        return {"kind": "github", "path": GITHUB_OWN_WORKFLOW, "create": True}
    if (repo / ".gitlab-ci.yml").is_file():
        return {"kind": "gitlab", "path": ".gitlab-ci.yml"}
    if _is_gitlab_host(_origin_url(repo)):
        return {"kind": "gitlab", "path": ".gitlab-ci.yml", "create": True}
    return {"kind": "none", "path": None}


GITHUB_OWN_WORKFLOW = ".github/workflows/ground-truth.yml"


def _is_gitlab_host(url: str) -> bool:
    """The remote's host names gitlab. Only the host counts: a GitHub repo
    called gitlab-exporter is still GitHub. A self-hosted GitLab without
    "gitlab" in its host name is not recognised; pass --ci-file for it."""
    m = re.search(r"(?:@|//)([^/:]+)", url)
    return bool(m) and "gitlab" in m.group(1).lower()


def _origin_url(repo: Path) -> str:
    """git remote get-url origin, or "" when there is no such remote."""
    try:
        cp = subprocess.run(
            ["git", "remote", "get-url", "origin"], cwd=repo, capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return cp.stdout.strip() if cp.returncode == 0 else ""


_CACHE_PREFIXES = ("$CI_PROJECT_DIR/", "${CI_PROJECT_DIR}/", "$GITHUB_WORKSPACE/", "${{ github.workspace }}/")


class _TolerantLoader(yaml.SafeLoader):
    """SafeLoader that keeps going past custom tags such as !reference."""


_TolerantLoader.add_multi_constructor("!", lambda loader, suffix, node: None)


def _ci_cache_paths(kind: str, text: str) -> list[str]:
    """Cache paths the CI runner restores inside the checkout: gitlab
    `cache: paths:` entries at any level, github `actions/cache` `path:`
    values. Paths outside the checkout (absolute, ~, an env var other than
    the workspace) and glob patterns are dropped -- only a literal path can
    be checked against .gitignore."""
    try:
        doc = yaml.load(text, Loader=_TolerantLoader)
    except yaml.YAMLError:
        return []
    found: list[str] = []

    def walk(node) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if kind == "gitlab" and key == "cache":
                    entries = value if isinstance(value, list) else [value]
                    for entry in entries:
                        paths = entry.get("paths") if isinstance(entry, dict) else None
                        for p in paths or []:
                            found.append(str(p))
                elif kind == "github" and key == "uses" and str(value).startswith("actions/cache"):
                    with_ = node.get("with")
                    if isinstance(with_, dict):
                        for p in str(with_.get("path") or "").splitlines():
                            found.append(p.strip())
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(doc)
    out: list[str] = []
    for p in found:
        for prefix in _CACHE_PREFIXES:
            if p.startswith(prefix):
                p = p[len(prefix):]
        if p.startswith("./"):
            p = p[2:]
        if not p or p.startswith(("/", "~", "$")) or any(ch in p for ch in "*?[{"):
            continue
        if p not in out:
            out.append(p)
    return out


def _unignored(repo: Path, paths: list[str]) -> list[str]:
    """Subset of paths .gitignore does not cover (git check-ignore is
    read-only). Outside a git repo every path counts as unignored."""
    result = []
    for p in paths:
        cp = subprocess.run(["git", "check-ignore", "-q", "--", p], cwd=repo, capture_output=True, text=True)
        if cp.returncode != 0:
            result.append(p)
    return result


def _is_gt_artifact(rel: str) -> bool:
    """True for the files under .claude/ that i9_install itself writes."""
    name = Path(rel).name
    return rel == ".claude/rules/ground-truth.md" or (
        rel.startswith(".claude/hooks/") and name.startswith("ground-truth-")
    )


def gather_facts(repo: Path, ci_file: str | None = None) -> dict:
    """Collect every fact i9_install needs before touching the repo (SKILL.md #5).

    ci_file, when given, names the repo-relative workflow (or .gitlab-ci.yml)
    to use instead of the alphabetically first .github/workflows/*.yml --
    the auto-pick is wrong whenever a repo ships more than one workflow.
    Raises FileNotFoundError if ci_file does not exist: a flag naming a
    specific file is a claim about the repo, not a hint, and a typo must
    not be read as "no CI file present".
    """
    # D25 asks whether the repo tracked .claude/ before ground-truth arrived;
    # the rule file and hooks this installer writes must not answer "yes"
    # on their own, or a local install flips to committed on the next audit.
    claude_tracked = bool([p for p in ls_files(repo, ".claude") if not _is_gt_artifact(p)])
    hooks_dir = ".claude/hooks" if claude_tracked else "tools/ground_truth/hooks"
    settings_file = ".claude/settings.json" if claude_tracked else ".claude/settings.local.json"
    ci = _detect_ci(repo, ci_file)
    ci_path = repo / ci["path"] if ci["path"] else None
    cache_paths = (
        _ci_cache_paths(ci["kind"], ci_path.read_text(encoding="utf-8")) if ci_path and ci_path.is_file() else []
    )
    return {
        "claude_tracked": claude_tracked,
        "hooks_dir": hooks_dir,
        "settings_file": settings_file,
        "ci": ci,
        # a cache restored into the checkout is an untracked directory the
        # banned-word scan reads; pip metadata alone carries every vendor name
        "ci_cache_paths": cache_paths,
        "ci_cache_unignored": _unignored(repo, cache_paths),
        "pre_commit": (repo / ".pre-commit-config.yaml").is_file(),
        "python_runner": _detect_python_runner(repo),
        "go_mod": (repo / "go.mod").is_file(),
        "js": _detect_js_runner(repo),
        "maven": _detect_maven(repo),
        "venv_python": (repo / ".venv" / "bin" / "python").exists(),
        "coverage_other_extensions": _coverage_other_extensions(repo),
    }


def _coverage_other_extensions(repo: Path) -> str | None:
    """The verifier's WARN about files under meta.coverage.roots that
    meta.coverage.extensions leaves out, when STATUS.yaml already exists."""
    status = repo / "STATUS.yaml"
    if not status.is_file():
        return None
    templates = str(Path(__file__).resolve().parent.parent.parent / "templates")
    sys.path.insert(0, templates)
    saved_flag = sys.dont_write_bytecode
    sys.dont_write_bytecode = True  # templates/ ships to repos, keep it free of __pycache__
    try:
        import contract_lib  # noqa: E402

        data = yaml.safe_load(status.read_text(encoding="utf-8")) or {}
        issues = contract_lib.check_coverage(repo, data, "sync") if isinstance(data, dict) else []
    except Exception:  # a broken STATUS.yaml is verify's to report, not this fact's
        return None
    finally:
        sys.dont_write_bytecode = saved_flag
        sys.path.remove(templates)
    for issue in issues:
        if "extensions outside meta.coverage.extensions" in issue.message:
            return issue.message
    return None


# ------------------------------------------------------------- file sync --

def _chmod_x(path: Path) -> None:
    mode = path.stat().st_mode
    path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def sync_file(src: Path, dst: Path, write: bool, chmod_x: bool = False, transform=None) -> str:
    """Copy src to dst (optionally transformed) if its bytes differ. Returns a verb."""
    if not src.is_file():
        return "MISSING-SOURCE"
    data = src.read_bytes()
    if transform is not None:
        data = transform(data)
    if dst.is_file():
        verb = "OK" if dst.read_bytes() == data else "UPDATE"
    else:
        verb = "CREATE"
    if write:
        if verb != "OK":
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(data)
        if chmod_x:
            _chmod_x(dst)
    return verb


def _go_package_name(pkg_dir: Path) -> str | None:
    if not pkg_dir.is_dir():
        return None
    for f in sorted(pkg_dir.glob("*.go")):
        m = re.search(r"^package\s+(\w+)", f.read_text(encoding="utf-8", errors="ignore"), re.M)
        if m:
            return m.group(1)
    return None


def _resolve_rule(data: bytes, py: str) -> bytes:
    text = data.decode("utf-8")
    text = _INSTALLER_COMMENT_RE.sub("", text, count=1)
    text = text.replace("{{PY}}", py)
    return text.encode("utf-8")


# ----------------------------------------------------- settings / CI / pc --

def _settings_addition(templates_dir: Path, hooks_dir: str) -> dict:
    data = json.loads((templates_dir / "settings-hooks.json").read_text(encoding="utf-8"))
    if hooks_dir != ".claude/hooks":
        text = json.dumps(data).replace(".claude/hooks/", hooks_dir.rstrip("/") + "/")
        data = json.loads(text)
    return data["hooks"]


def _command_key(command) -> str:
    """The part of a hook command the shell runs: a trailing `# ...` comment
    is dropped, so an older settings entry written without the comment and
    the template's commented one count as the same hook."""
    return re.sub(r"\s+#.*$", "", str(command or "")).strip()


def merge_settings(existing: dict, addition: dict) -> tuple[dict, bool]:
    """Merge hook groups into existing by command string; never replace an
    array. A hook already present keeps its entry, except that its timeout
    is raised to the template's value when the template asks for more
    (a hand-raised timeout is never lowered), and its group's matcher is
    replaced with the template's when they differ."""
    merged = json.loads(json.dumps(existing)) if existing else {}
    hooks = merged.setdefault("hooks", {})
    changed = False
    for event, groups in addition.items():
        event_list = hooks.setdefault(event, [])
        present = {_command_key(h.get("command")): (g, h) for g in event_list for h in g.get("hooks", [])}
        for group in groups:
            new_hooks = group.get("hooks", [])
            matched = [(present[_command_key(h.get("command"))], h) for h in new_hooks if _command_key(h.get("command")) in present]
            if matched:
                for (cur_group, cur), tpl in matched:
                    if tpl.get("timeout", 0) > cur.get("timeout", 0):
                        cur["timeout"] = tpl["timeout"]
                        changed = True
                    if cur_group.get("matcher") != group.get("matcher"):
                        cur_group["matcher"] = group.get("matcher")
                        changed = True
                continue
            event_list.append(group)
            present.update({_command_key(h.get("command")): (group, h) for h in new_hooks})
            changed = True
    return merged, changed


def plan_settings(repo: Path, facts: dict, templates_dir: Path, write: bool) -> tuple[str, str]:
    dst = repo / facts["settings_file"]
    rel = facts["settings_file"]
    addition = _settings_addition(templates_dir, facts["hooks_dir"])
    existing = {}
    if dst.is_file():
        try:
            existing = json.loads(dst.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return "FAIL", f"{rel} is not valid JSON, not touching it"
    merged, changed = merge_settings(existing, addition)
    verb = "OK" if not changed else ("UPDATE" if existing else "CREATE")
    if write and changed:
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(json.dumps(merged, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return verb, rel


# The verifier commands the CI job has to run. Everything else in the CI
# templates -- dependency installs, env -- is an example each repo adapts, so
# only these decide whether a job that is already in the file is up to date,
# and only these get appended to a job installed before they existed.
CI_VERIFY_COMMAND = "tools/ground_truth/verify.py --mode=full"
CI_EXTRA_COMMANDS = [
    ("# project memory: missing model, stale evidence or missing repositories fail closed",
     "tools/ground_truth/gt_context.py check"),
    ("# citations in STATUS.yaml must still point at the right line after this diff",
     'tools/ground_truth/remap_line_refs.py --check --base "${GT_BASE_REF:-HEAD~1}" STATUS.yaml'),
    ("# canaries: every claim carrying a mutation still goes red under it",
     "tools/ground_truth/gt_mutation_selfcheck.py --strict"),
    ("# same three rules the Stop hook runs, against the pushed diff",
     "tools/ground_truth/gt_session_guard.py --mode ci"),
]
# Both job names end in this; the github one is verify-status-contract.
CI_JOB_MARKER = "status-contract:"
_CI_VERIFY_LINE_RE = re.compile(
    r"^(?P<indent> *)(?P<lead>- )?(?P<run>run: )?(?P<py>\S+) "
    + re.escape(CI_VERIFY_COMMAND) + r"\s*$"
)


def _ci_missing_commands(text: str) -> list[tuple[str, str]]:
    return [(comment, cmd) for comment, cmd in CI_EXTRA_COMMANDS if cmd not in text]


def _ci_add_commands(text: str) -> str | None:
    """Add the verifier commands an already-installed CI job does not run yet,
    next to the verify.py line it does run. Returns None when that line is not
    in a shape this can extend, or when the result would not parse as YAML --
    such a job is hand-written enough that guessing is worse than saying so.
    """
    missing = _ci_missing_commands(text)
    if not missing:
        return text
    lines = text.splitlines(keepends=True)
    for i in range(len(lines) - 1, -1, -1):
        m = _CI_VERIFY_LINE_RE.match(lines[i].rstrip("\n"))
        if m is None:
            continue
        indent, py = m.group("indent"), m.group("py")
        if m.group("run"):
            # A one-line "run:" scalar has nowhere to put a second command,
            # so it becomes a block with the existing command at its top.
            lead = m.group("lead") or ""
            body = indent + " " * (len(lead) + 2)
            block = [f"{indent}{lead}run: |\n", f"{body}{py} {CI_VERIFY_COMMAND}\n"]
            cmd_indent = comment_indent = body
        else:
            block = [lines[i] if lines[i].endswith("\n") else lines[i] + "\n"]
            cmd_indent = indent + (m.group("lead") or "")
            comment_indent = indent
        for comment, cmd in missing:
            block.append(f"{comment_indent}{comment}\n")
            block.append(f"{cmd_indent}{py} {cmd}\n")
        new_text = "".join(lines[:i]) + "".join(block) + "".join(lines[i + 1:])
        try:
            yaml.safe_load(new_text)
        except yaml.YAMLError:
            return None
        return new_text
    return None


def _plan_ci_existing(path: Path, rel: str, text: str, write: bool) -> tuple[str, str]:
    """The job is already in this CI file: bring the commands it runs up to
    date instead of reporting OK on the job name alone, otherwise a command
    added to the templates never reaches a repo that installed once."""
    missing = _ci_missing_commands(text)
    if not missing:
        return "OK", rel
    new_text = _ci_add_commands(text)
    if new_text is None:
        return "FAIL", (
            f"{rel}: the job is present but its {CI_VERIFY_COMMAND} line could not be "
            f"extended, add these manually: {', '.join(cmd for _, cmd in missing)}"
        )
    if write:
        path.write_text(new_text, encoding="utf-8")
    return "UPDATE", rel


def _strip_comment_lines(text: str) -> str:
    lines = text.splitlines(keepends=True)
    i = 0
    while i < len(lines) and lines[i].lstrip().startswith("#"):
        i += 1
    return "".join(lines[i:])


_MAVEN_IMAGE_DEFAULT = "maven:3.9-eclipse-temurin-21"
_GITLAB_PYTHON_IMAGE = "python:3.12-slim"


def _maven_ci_addition(repo_ci_text: str, addition: str) -> str:
    """Java repos need a maven image and no pip -- SKILL.md #5."""
    image = _MAVEN_IMAGE_DEFAULT
    for m in re.finditer(r"(?m)^\s*image:\s*(.+?)\s*$", repo_ci_text):
        if "maven" in m.group(1):
            image = m.group(1)
            break
    addition = re.sub(
        r"(?m)^(status-contract:[ \t]*\r?\n)",
        lambda m: m.group(1) + f"  image: {image}\n",
        addition,
        count=1,
    )
    return addition.replace(
        "    - pip install -r requirements-dev.txt",
        "    - apt-get update -qq && apt-get install -y -qq python3 python3-yaml",
    )


_PIP_PROJECT_LINE = "pip install -r requirements-dev.txt"
_PIP_VERIFIER_LINE = "pip install pyyaml pytest"


def _pip_line(addition: str, facts: dict) -> str:
    """A repo with no python test runner has no requirements-dev.txt to
    install; the verifier itself needs only pyyaml and pytest."""
    if facts["python_runner"]["detected"]:
        return addition
    return addition.replace(_PIP_PROJECT_LINE, _PIP_VERIFIER_LINE)


def _gitlab_addition(templates_dir: Path, facts: dict, repo_ci_text: str) -> str:
    addition = _strip_comment_lines((templates_dir / "ci-gitlab-addition.yml").read_text(encoding="utf-8"))
    if facts["maven"]["present"]:
        addition = _maven_ci_addition(repo_ci_text, addition)
    return _pip_line(addition, facts)


def plan_ci(repo: Path, facts: dict, templates_dir: Path, write: bool) -> tuple[str, str]:
    ci = facts["ci"]
    if ci["kind"] == "gitlab" and ci.get("create"):
        # No CI file yet and origin is a GitLab remote: the job becomes the
        # whole .gitlab-ci.yml (the "test" stage it names is a GitLab default).
        rel = ci["path"]

        def transform(data: bytes) -> bytes:
            text = _gitlab_addition(templates_dir, facts, "")
            if not facts["maven"]["present"]:
                # A new file has no default image to inherit; the maven
                # branch already set its own.
                text = re.sub(
                    r"(?m)^(status-contract:[ \t]*\r?\n)",
                    lambda m: m.group(1) + f"  image: {_GITLAB_PYTHON_IMAGE}\n",
                    text,
                    count=1,
                )
            return text.encode("utf-8")

        return sync_file(templates_dir / "ci-gitlab-addition.yml", repo / rel, write, transform=transform), rel
    if ci["kind"] == "github" and not ci.get("create"):
        rel = ci["path"]
        path = repo / rel
        text = path.read_text(encoding="utf-8")
        if "verify-status-contract:" in text:
            return _plan_ci_existing(path, rel, text, write)
        m = re.search(r"(?m)^jobs:[ \t]*\r?\n", text)
        if not m:
            return "FAIL", f"{rel}: no top-level 'jobs:' key found, add the job manually"
        addition = _strip_comment_lines((templates_dir / "ci-github-job-addition.yml").read_text(encoding="utf-8"))
        addition = _pip_line(addition, facts)
        new_text = text[:m.end()] + addition + text[m.end():]
        if write:
            path.write_text(new_text, encoding="utf-8")
        return "UPDATE", rel
    if ci["kind"] == "gitlab":
        rel = ci["path"]
        path = repo / rel
        text = path.read_text(encoding="utf-8")
        if re.search(r"(?m)^status-contract:", text):
            return _plan_ci_existing(path, rel, text, write)
        addition = _gitlab_addition(templates_dir, facts, text)
        new_text = text.rstrip("\n") + "\n\n" + addition
        if write:
            path.write_text(new_text, encoding="utf-8")
        return "UPDATE", rel
    rel = GITHUB_OWN_WORKFLOW
    dst = repo / rel
    transform = lambda data: _pip_line(data.decode("utf-8"), facts).encode("utf-8")
    verb = sync_file(templates_dir / "ci-github.yml", dst, write, transform=transform)
    return verb, rel


def plan_gitignore(repo: Path, facts: dict, write: bool) -> tuple[str, str] | None:
    """A CI cache restored inside the checkout is an untracked directory, and
    the banned-word scan reads untracked files: third-party package metadata
    under a pip cache is full of vendor names. Ignore such paths."""
    if not facts["ci_cache_paths"]:
        return None
    missing = facts["ci_cache_unignored"]
    if not missing:
        return "OK", ".gitignore (CI cache paths inside the checkout are ignored)"
    path = repo / ".gitignore"
    verb = "UPDATE" if path.is_file() else "CREATE"
    if write:
        existing = path.read_text(encoding="utf-8") if path.is_file() else ""
        if existing and not existing.endswith("\n"):
            existing += "\n"
        block = (
            "# CI cache restored into the checkout; the status-contract job scans untracked files.\n"
            + "".join(p + "\n" for p in missing)
        )
        path.write_text(existing + ("\n" if existing else "") + block, encoding="utf-8")
    return verb, (
        f".gitignore (+ {', '.join(missing)}: CI cache path inside the checkout, "
        "the untracked-file scan would read it)"
    )


def _repos_list_end(text: str) -> tuple[int, int] | None:
    """Find the top-level ``repos:`` list.

    Returns (insertion_line, item_indent): insertion_line is the line
    index right after the list's last item (and before the next
    top-level key, or EOF); item_indent is the indentation the repo's
    own ``- repo:`` entries use, so a pasted-in entry lines up with
    them instead of landing at column 0.  None if no ``repos:`` list
    is found.
    """
    lines = text.splitlines(keepends=True)
    repos_indent = None
    repos_idx = None
    for i, line in enumerate(lines):
        m = re.match(r"^( *)repos:[ \t]*(#.*)?$", line)
        if m:
            repos_idx = i
            repos_indent = len(m.group(1))
            break
    if repos_idx is None:
        return None
    item_indent = None
    end = len(lines)
    for j in range(repos_idx + 1, len(lines)):
        line = lines[j]
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        is_item = bool(re.match(r"^ *-\s", line))
        if indent < repos_indent:
            end = j
            break
        if indent == repos_indent:
            if is_item:
                # list items at the same column as "repos:" itself
                # (compact style) are still part of the list.
                if item_indent is None:
                    item_indent = indent
                continue
            end = j
            break
        if is_item and item_indent is None:
            item_indent = indent
    if item_indent is None:
        item_indent = repos_indent + 2
    return end, item_indent


def _reindent_block(text: str, indent: int) -> str:
    pad = " " * indent
    return "".join(pad + line if line.strip() else line for line in text.splitlines(keepends=True))


def plan_precommit(repo: Path, facts: dict, templates_dir: Path, write: bool) -> tuple[str, str]:
    if not facts["pre_commit"]:
        return "SKIP", ".pre-commit-config.yaml (not present, not creating it for this)"
    rel = ".pre-commit-config.yaml"
    path = repo / rel
    text = path.read_text(encoding="utf-8")
    if "id: ground-truth-verify" in text:
        return "OK", rel

    block = _repos_list_end(text)
    if block is None:
        return "FAIL", f"{rel}: no top-level 'repos:' list found, add the entry manually"
    end, item_indent = block

    raw_addition = _strip_comment_lines((templates_dir / "pre-commit-addition.yaml").read_text(encoding="utf-8"))
    addition = _reindent_block(raw_addition, item_indent)

    lines = text.splitlines(keepends=True)
    if lines and end > 0 and not lines[end - 1].endswith("\n"):
        lines[end - 1] += "\n"
    new_text = "".join(lines[:end]) + addition + "".join(lines[end:])

    try:
        yaml.safe_load(new_text)
    except yaml.YAMLError as exc:
        return "FAIL", f"{rel}: inserting the entry would break the YAML ({exc}), add it manually"

    if write:
        path.write_text(new_text, encoding="utf-8")
    return "UPDATE", rel


# --------------------------------------------------------------- drift --

_STATUS_BY_VERB = {"OK": "same", "UPDATE": "outdated", "CREATE": "missing", "MISSING-SOURCE": "missing"}


def _ci_job_drift(repo: Path, facts: dict) -> dict:
    """The CI job counts as installed when it runs every command in
    CI_EXTRA_COMMANDS -- the same test plan_ci applies. Byte or whole-template
    comparison would report a repo outdated forever: the templates tell each
    repo to replace the example dependency and env lines with its own.
    """
    ci = facts["ci"]
    rel = ci["path"] or GITHUB_OWN_WORKFLOW
    target = repo / rel
    if not target.is_file():
        return {"path": rel, "status": "missing"}
    text = target.read_text(encoding="utf-8")
    if CI_JOB_MARKER not in text or CI_VERIFY_COMMAND not in text or _ci_missing_commands(text):
        return {"path": rel, "status": "outdated"}
    return {"path": rel, "status": "same"}


def _settings_drift(repo: Path, facts: dict, templates_dir: Path) -> dict:
    """Presence of every hook command, not byte equality: the settings file
    holds the repo's own hooks too and i9_install only merges into it. The
    commands come from _settings_addition, so a repo whose .claude is
    untracked is checked for the tools/ground_truth/hooks/ paths that were
    actually installed there.
    """
    addition = _settings_addition(templates_dir, facts["hooks_dir"])
    commands = [h["command"] for groups in addition.values() for g in groups for h in g["hooks"]]
    rel = facts["settings_file"]
    target = repo / rel
    if not target.is_file():
        return {"path": rel, "status": "missing"}
    text = target.read_text(encoding="utf-8")
    return {"path": rel, "status": "same" if all(_command_key(c) in text for c in commands) else "outdated"}


def drift_report(repo: Path, facts: dict, templates_dir: Path) -> list[dict]:
    """Classify every artifact i9_install would sync as same/outdated/missing.

    Byte comparison (via sync_file, write=False -- nothing is touched) for
    TOOLS_FILES, the example probe, HOOK_FILES and the rule file (the same
    _resolve_rule transform i9_install applies); a command-presence check for
    the CI job and the settings hooks block, since i9_install inserts those
    into a file it does not fully own. install and audit share this one
    definition so the two never classify a file differently.
    """
    report: list[dict] = []

    tools_dir = repo / "tools" / "ground_truth"
    for name in TOOLS_FILES:
        verb = sync_file(templates_dir / name, tools_dir / name, write=False)
        report.append({"path": f"tools/ground_truth/{name}", "status": _STATUS_BY_VERB[verb]})

    verb = sync_file(templates_dir / "probes" / EXAMPLE_PROBE, tools_dir / "probes" / EXAMPLE_PROBE, write=False)
    report.append({"path": f"tools/ground_truth/probes/{EXAMPLE_PROBE}", "status": _STATUS_BY_VERB[verb]})

    hooks_dir = repo / facts["hooks_dir"]
    for name in HOOK_FILES:
        verb = sync_file(templates_dir / name, hooks_dir / name, write=False)
        report.append({"path": f"{facts['hooks_dir']}/{name}", "status": _STATUS_BY_VERB[verb]})

    py_placeholder = ".venv/bin/python" if facts["venv_python"] else "python3"
    rule_transform = lambda data, py=py_placeholder: _resolve_rule(data, py)
    verb = sync_file(templates_dir / RULE_FILE, repo / ".claude" / "rules" / "ground-truth.md",
                      write=False, transform=rule_transform)
    report.append({"path": ".claude/rules/ground-truth.md", "status": _STATUS_BY_VERB[verb]})

    report.append(_ci_job_drift(repo, facts))
    report.append(_settings_drift(repo, facts, templates_dir))
    return report


# ------------------------------------------------------------------ main --

def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run", required=True, help="path to run.json")
    parser.add_argument("--facts", action="store_true", help="print install-relevant facts as JSON and exit")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="print the plan without writing (default)")
    mode.add_argument("--write", action="store_true", help="apply the plan")
    parser.add_argument("--pkg", help="repo-relative Go package directory to receive the Go bridge test")
    parser.add_argument("--js-test-dir", help="repo-relative directory to receive the JS/TS bridge test")
    parser.add_argument("--java-pkg", help="dotted Java package to receive the JUnit bridge test, overriding auto-detection")
    parser.add_argument(
        "--ci-file",
        help="repo-relative workflow file (or .gitlab-ci.yml) to receive the job, "
        "overriding auto-detection when the repo has more than one workflow; "
        ".gitlab-ci.yml is created when missing (a GitLab host without 'gitlab' in its name)",
    )
    args = parser.parse_args(argv)

    run = load_run(args.run)
    try:
        facts = gather_facts(run.repo, ci_file=args.ci_file)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    js_warn = facts["js"]["present"] and facts["js"]["runner"] is None
    if args.facts:
        print(json.dumps(facts, indent=2))
        if js_warn:
            print(JS_NO_RUNNER_WARN, file=sys.stderr)
        return 0

    write = bool(args.write)
    templates_dir = run.skill / "templates"
    report: list[tuple[str, str]] = []

    tools_dir = run.repo / "tools" / "ground_truth"
    for name in TOOLS_FILES:
        verb = sync_file(templates_dir / name, tools_dir / name, write)
        report.append((verb, f"tools/ground_truth/{name}"))

    verb = sync_file(templates_dir / "probes" / EXAMPLE_PROBE, tools_dir / "probes" / EXAMPLE_PROBE, write, chmod_x=True)
    report.append((verb, f"tools/ground_truth/probes/{EXAMPLE_PROBE}"))

    if facts["python_runner"]["detected"]:
        tests_dir = facts["python_runner"]["tests_dir"]
        verb = sync_file(templates_dir / PY_BRIDGE, run.repo / tests_dir / "test_status_contract.py", write)
        report.append((verb, f"{tests_dir}/test_status_contract.py"))
    else:
        report.append(("SKIP", "tests/test_status_contract.py (no python runner detected)"))

    if facts["go_mod"]:
        if not args.pkg:
            report.append(("SKIP", "status_contract_test.go (go.mod present, pass --pkg to install)"))
        else:
            pkg_dir = run.repo / args.pkg
            pkg_name = _go_package_name(pkg_dir) or (pkg_dir.name or "main")
            transform = lambda data, n=pkg_name: data.replace(b"package PACKAGE_NAME", f"package {n}".encode())
            verb = sync_file(templates_dir / GO_BRIDGE, pkg_dir / "status_contract_test.go", write, transform=transform)
            report.append((verb, f"{args.pkg}/status_contract_test.go"))

    if facts["maven"]["present"]:
        if not facts["maven"]["test_root"]:
            report.append(("SKIP", "StatusContractTest.java (pom.xml present, no src/test/java)"))
        else:
            pkg = args.java_pkg or facts["maven"]["package"]
            if pkg == ".":
                pkg = None
            if not pkg:
                report.append(("SKIP", "StatusContractTest.java (pom.xml present, pass --java-pkg to install)"))
            else:
                pkg_dir = run.repo / "src" / "test" / "java" / Path(*pkg.split("."))
                if not pkg_dir.is_dir():
                    report.append(("FAIL", f"--java-pkg {pkg}: src/test/java/{pkg.replace('.', '/')} does not exist"))
                else:
                    transform = lambda data, n=pkg: data.replace(b"package PACKAGE_NAME", f"package {n}".encode())
                    dst = pkg_dir / "StatusContractTest.java"
                    verb = sync_file(templates_dir / JAVA_BRIDGE, dst, write, transform=transform)
                    report.append((verb, str(dst.relative_to(run.repo))))

    if facts["js"]["runner"]:
        if not args.js_test_dir:
            report.append(("SKIP", "status_contract.test.ts (jest/vitest present, pass --js-test-dir to install)"))
        else:
            dst = run.repo / args.js_test_dir / "status_contract.test.ts"
            verb = sync_file(templates_dir / TS_BRIDGE, dst, write)
            report.append((verb, f"{args.js_test_dir}/status_contract.test.ts"))


    hooks_dir = run.repo / facts["hooks_dir"]
    for name in HOOK_FILES:
        verb = sync_file(templates_dir / name, hooks_dir / name, write, chmod_x=True)
        report.append((verb, f"{facts['hooks_dir']}/{name}"))

    py_placeholder = ".venv/bin/python" if facts["venv_python"] else "python3"
    rule_transform = lambda data, py=py_placeholder: _resolve_rule(data, py)
    verb = sync_file(templates_dir / RULE_FILE, run.repo / ".claude" / "rules" / "ground-truth.md", write, transform=rule_transform)
    report.append((verb, ".claude/rules/ground-truth.md"))

    verb, detail = plan_settings(run.repo, facts, templates_dir, write)
    report.append((verb, detail))

    verb, detail = plan_ci(run.repo, facts, templates_dir, write)
    report.append((verb, detail))

    planned = plan_gitignore(run.repo, facts, write)
    if planned:
        report.append(planned)

    verb, detail = plan_precommit(run.repo, facts, templates_dir, write)
    report.append((verb, detail))

    mode = "applied" if write else "planned (pass --write to apply)"
    print(f"ground-truth install, {mode}:")
    for verb, detail in report:
        print(f"  {verb:14s} {detail}")
    if js_warn:
        print(JS_NO_RUNNER_WARN)

    if any(v == "FAIL" for v, _ in report):
        return 1
    if any(v == "MISSING-SOURCE" for v, _ in report):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
