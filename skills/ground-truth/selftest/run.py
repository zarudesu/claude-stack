#!/usr/bin/env python3
"""End-to-end proof for the ground-truth verifier templates.

Builds a throwaway git repo fixture, copies the templates into it, writes a
valid STATUS.yaml, commits, and checks the happy path (verify sync/full,
the pytest wrapper, the mutation selfcheck). Then it applies each of a set
of planted faults in isolation and checks that verify (or the mutation
selfcheck) catches it with the right severity and mode.

Exit 0 only if every case passes. On failure the fixture directory is kept
and its path is printed; on success it is removed.
"""
from __future__ import annotations

import contextlib
import datetime
import json
import os
import shutil
import re
import subprocess
import sys
import tempfile
from pathlib import Path

# The banned-word self scan imports contract_lib straight out of templates/;
# never leave a __pycache__ behind in the directory that ships to repos.
sys.dont_write_bytecode = True

HERE = Path(__file__).resolve().parent
SKILL_ROOT = HERE.parent
TEMPLATES = SKILL_ROOT / "templates"
# GT_SELFTEST_TMP overrides where the throwaway fixture is built; unset means
# the system temp directory.
SCRATCH_BASE = Path(tempfile.mkdtemp(prefix="gt-selftest-", dir=os.environ.get("GT_SELFTEST_TMP")))
FIXTURE = SCRATCH_BASE / "fixture"

TODAY = datetime.date.today().isoformat()

RESULTS: list[tuple[str, bool, str]] = []


def record(label: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((label, ok, detail))
    tag = "PASS" if ok else "FAIL"
    line = f"{tag}: {label}"
    if not ok and detail:
        line += f" -- {detail}"
    print(line)
    return ok


def run(args: list[str], cwd: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, *args] if args[0].endswith(".py") else args,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )


def out(cp: subprocess.CompletedProcess) -> str:
    return cp.stdout + cp.stderr


def verify(
    mode: str, env: dict[str, str] | None = None, extra: list[str] | None = None
) -> subprocess.CompletedProcess:
    return run(["tools/ground_truth/verify.py", f"--mode={mode}", *(extra or [])], cwd=FIXTURE, env=env)


def mutation_selfcheck(claim_id: str | None = None, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    args = ["tools/ground_truth/gt_mutation_selfcheck.py"]
    if claim_id:
        args += ["--id", claim_id]
    return run(args, cwd=FIXTURE, env=env)


@contextlib.contextmanager
def patched(path: Path, old: str, new: str):
    original = path.read_text()
    assert old in original, f"anchor not found in {path}: {old!r}"
    path.write_text(original.replace(old, new, 1))
    try:
        yield
    finally:
        path.write_text(original)


@contextlib.contextmanager
def appended(path: Path, extra: str):
    original = path.read_text()
    path.write_text(original + extra)
    try:
        yield
    finally:
        path.write_text(original)


@contextlib.contextmanager
def added_file(path: Path, content: str):
    path.write_text(content)
    try:
        yield
    finally:
        path.unlink()


@contextlib.contextmanager
def removed_file(path: Path):
    original = path.read_bytes()
    path.unlink()
    try:
        yield
    finally:
        path.write_bytes(original)


# ---------------------------------------------------------------------------
# fixture sources
# ---------------------------------------------------------------------------

SOURCES: dict[str, str] = {
    # Without this the bytecode caches pytest writes show up as untracked
    # paths under the coverage roots, exactly as they would in a real
    # repository that forgot to ignore them.
    ".gitignore": "__pycache__/\n*.pyc\n",
    "services/alpha/__init__.py": "",
    "services/alpha/core.py": (
        'GREETING = "hello"\n'
        "\n"
        "\n"
        "def greet() -> str:\n"
        "    return GREETING\n"
    ),
    "services/beta/__init__.py": "",
    "services/beta/worker.py": (
        "def process() -> None:\n"
        '    raise NotImplementedError("worker.process is not implemented yet")\n'
    ),
    "handlers/__init__.py": "",
    "handlers/http.py": ('def handle() -> str:\n' '    return "ok"\n'),
    "main.py": (
        "from handlers.http import handle\n"
        "\n"
        "\n"
        "def main() -> str:\n"
        "    return handle()\n"
        "\n"
        "\n"
        'if __name__ == "__main__":\n'
        "    print(main())\n"
    ),
    "tests/__init__.py": "",
    "tests/test_alpha.py": (
        "from services.alpha import core\n"
        "\n"
        "\n"
        "def test_greet_returns_greeting():\n"
        '    assert core.greet() == "hello"\n'
        "\n"
        "\n"
        "def test_unrelated_passes():\n"
        "    assert 1 == 1\n"
        "\n"
        "\n"
        "def test_records_nothing():\n"
        "    core.greet()\n"
    ),
    "tests/test_beta.py": (
        "import pytest\n"
        "\n"
        "from services.beta import worker\n"
        "\n"
        "\n"
        "def test_process_not_implemented():\n"
        "    with pytest.raises(NotImplementedError):\n"
        "        worker.process()\n"
    ),
    "tests/test_handlers.py": (
        "from handlers import http\n"
        "\n"
        "\n"
        "def test_handle_returns_ok():\n"
        '    assert http.handle() == "ok"\n'
    ),
    "tests/test_main.py": (
        "import main as main_module\n"
        "\n"
        "\n"
        "def test_main_runs():\n"
        '    assert main_module.main() == "ok"\n'
    ),
    "go.mod": "module gtselftest\n\ngo 1.22\n",
    "svc/majority.go": (
        "// Package svc decides a majority verdict.\n"
        "package svc\n"
        "\n"
        "// Verdict reports which side has more votes.\n"
        "func Verdict(yes, no int) string {\n"
        "\tif yes > no {\n"
        '\t\treturn "yes"\n'
        "\t}\n"
        '\treturn "no"\n'
        "}\n"
    ),
    "svc/majority_test.go": (
        "package svc\n"
        "\n"
        'import "testing"\n'
        "\n"
        "func TestVerdictYes(t *testing.T) {\n"
        '\tif got := Verdict(3, 1); got != "yes" {\n'
        '\t\tt.Fatalf("expected yes, got %s", got)\n'
        "\t}\n"
        "}\n"
    ),
    "web/state.js": (
        "// Maps a logical state to a display colour.\n"
        "function colorFor(state) {\n"
        '  if (state === "yes") return "green";\n'
        '  return "gray";\n'
        "}\n"
        "\n"
        "module.exports = { colorFor };\n"
    ),
    "web/test_state.js": (
        'const test = require("node:test");\n'
        'const assert = require("node:assert");\n'
        'const { colorFor } = require("./state.js");\n'
        "\n"
        'test("yes is green", () => {\n'
        '  assert.strictEqual(colorFor("yes"), "green");\n'
        "});\n"
    ),
    "docs/_human/notes.md": "# Human notes\n\nArchive only, agents do not read this.\n",
    "README.md": "# gt-selftest-fixture\n\nThrowaway fixture repo for the ground-truth verifier selftest.\n",
    "tools/ground_truth/probes/smoke_probe.py": (
        "#!/usr/bin/env python3\n"
        '"""Smoke probe: the handlers module loads and behaves."""\n'
        "import sys\n"
        "\n"
        'sys.path.insert(0, ".")\n'
        "try:\n"
        "    from handlers import http\n"
        "except ImportError:\n"
        "    sys.exit(77)\n"
        'sys.exit(0 if http.handle() == "ok" else 1)\n'
    ),
}

STATUS_YAML = f"""\
schema_version: 1
meta:
  repo: "gt-selftest-fixture"
  enforcement: advisory
  last_audited: "{TODAY}"
  coverage:
    roots:
      - services
      - handlers
    exclude: []
    extensions:
      - .py
    runner: pytest

claims:
  - id: alpha_greeting
    component: "alpha service"
    kind: status
    status: implemented
    check_kind: pytest
    check: "tests/test_alpha.py::test_greet_returns_greeting"
    path: "services/alpha"
    note: "core.greet() returns the GREETING constant; the canary flips it to prove the test reads the constant."
    canary: true
    mutation:
      file: "services/alpha/core.py"
      find: 'GREETING = "hello"'
      replace: 'GREETING = "goodbye"'

  - id: beta_worker_stub
    component: "beta worker"
    kind: status
    status: stub
    check_kind: pytest
    check: "tests/test_beta.py::test_process_not_implemented"
    path: "services/beta"
    note: "worker.process() is not implemented; the test only characterizes the current NotImplementedError."

  - id: handlers_http
    component: "http handlers"
    kind: status
    status: implemented
    check_kind: pytest
    check: "tests/test_handlers.py::test_handle_returns_ok"
    path: "handlers"
    note: "handle() returns a fixed ok response."
    canary: true
    mutation:
      file: "handlers/http.py"
      find: 'return "ok"'
      replace: 'return "nope"'

  - id: svc_majority
    component: "majority service"
    kind: status
    status: implemented
    check_kind: go_test
    check: "svc::TestVerdictYes"
    path: "svc"
    note: "Verdict() picks the side with more votes; the canary flips the branch it returns."
    canary: true
    mutation:
      file: "svc/majority.go"
      find: 'return "yes"'
      replace: 'return "no"'

  - id: web_state_color
    component: "web state colours"
    kind: status
    status: implemented
    check_kind: js_test
    check: "web/test_state.js::yes is green"
    path: "web/state.js"
    note: "colorFor() maps the yes state to green."

  - id: main_script
    component: "entry script"
    kind: status
    status: implemented
    check_kind: pytest
    check: "tests/test_main.py::test_main_runs"
    path: "main.py"
    note: "main() delegates to handlers.http.handle()."

  - id: ops_smoke
    component: "ops"
    kind: status
    status: implemented
    check_kind: probe
    check: "tools/ground_truth/probes/smoke_probe.py"
    note: "wiring-only smoke probe confirming the handlers module loads."

  - id: alpha_health_live
    component: "alpha service"
    kind: live_state
    command: "true"
    note: "placeholder liveness command; a real repo would curl a health endpoint."

  - id: external_dashboard
    component: "ops"
    kind: out_of_repo
    pointer: "https://example.invalid/dashboard"
    note: "dashboard lives outside this repo."

edges:
  - from: alpha_greeting
    to: handlers_http
    probe: "tools/ground_truth/probes/example_probe.py"

debt:
  - id: debt_beta_stub
    ref: beta_worker_stub
    description: "worker.process is a stub, needs a real implementation"
    severity: normal
    state: open
    owner: ""
    opened: "{TODAY}"
"""

RAISE_FROM = "    status: stub\n"
RAISE_TO = "    status: implemented\n"
TOUCH_COMMENT = "\n# touched alongside the claim it proves\n"

BETA_BLOCK = (
    "    status: stub\n"
    "    check_kind: pytest\n"
    '    check: "tests/test_beta.py::test_process_not_implemented"\n'
)
BETA_BLOCK_RECHECKED = (
    "    status: implemented\n"
    "    check_kind: pytest\n"
    '    check: "tests/test_alpha.py::test_unrelated_passes"\n'
)

ALPHA_BLOCK = (
    "    status: implemented\n"
    "    check_kind: pytest\n"
    '    check: "tests/test_alpha.py::test_greet_returns_greeting"\n'
)

OLD_AUDIT_DATE = (datetime.date.today() - datetime.timedelta(days=60)).isoformat()

HANDLERS_BLOCK = (
    "    status: implemented\n"
    "    check_kind: pytest\n"
    '    check: "tests/test_handlers.py::test_handle_returns_ok"\n'
)
HANDLERS_CANARY = (
    "    canary: true\n"
    "    mutation:\n"
    '      file: "handlers/http.py"\n'
    "      find: 'return \"ok\"'\n"
    "      replace: 'return \"nope\"'\n"
)

ZERO_ASSERTION_CLAIM = (
    "  - id: alpha_records_nothing\n"
    '    component: "alpha service"\n'
    "    kind: status\n"
    "    status: implemented\n"
    "    check_kind: pytest\n"
    '    check: "tests/test_alpha.py::test_records_nothing"\n'
    '    note: "claim written in this change; its check asserts nothing at all."\n'
)

WORKER_TOUCH = "\n# touched alongside the claim it covers\n"
WORKER_RED = "def process() -> None:\n    return None\n"

HANDLERS_STUB_CANARY_CLAIM = (
    "  - id: handlers_http_second_stage\n"
    '    component: "http handlers"\n'
    "    kind: status\n"
    "    status: stub\n"
    "    check_kind: pytest\n"
    '    check: "tests/test_handlers.py::test_handle_returns_ok"\n'
    '    path: "handlers/http.py"\n'
    '    note: "the second stage of the handler is not written yet."\n'
    "    canary: true\n"
    "    mutation:\n"
    '      file: "handlers/http.py"\n'
    "      find: 'return \"ok\"'\n"
    "      replace: 'return \"nope\"'\n"
)
HANDLERS_STUB_DEBT = (
    "  - id: debt_handlers_second_stage\n"
    "    ref: handlers_http_second_stage\n"
    '    description: "the second stage of the handler is missing"\n'
    "    severity: normal\n"
    "    state: open\n"
    '    owner: ""\n'
    f'    opened: "{TODAY}"\n'
)

# A coverage root reached through a symlink: the files under it resolve to
# real/src, the contract spells the root src.
SYMLINK_STATUS_YAML = f"""\
schema_version: 1
meta:
  repo: "gt-selftest-symlink-root"
  enforcement: advisory
  last_audited: "{TODAY}"
  coverage:
    roots:
      - src
    exclude: []
    extensions:
      - .py
    runner: pytest

claims:
  - id: covered_f
    component: "covered module"
    kind: status
    status: implemented
    check_kind: pytest
    check: "tests/test_c.py::test_f"
    path: "src/covered.py"
    note: "f() returns the hi literal and the test pins it."
    canary: true
    mutation:
      file: "real/src/covered.py"
      find: 'return "hi"'
      replace: 'return "bye"'

edges: []

debt: []
"""

# The commented example of templates/STATUS.yaml.template plus a live copy
# of the same line, comment and all.
AUDIT_CLOSE_STATUS = f"""\
schema_version: 1
meta:
  repo: "gt-selftest-audit-close"
  last_audited: "{TODAY}"
  # last_audited_commit: "9f2c1ab"  # optional; sha audited at, written by the audit close
                                    # step -- how many files changed since then decides
                                    # whether the next audit is already due
  last_audited_commit: "9f2c1ab"  # optional; sha audited at, written by the audit close
claims: []
"""


def toolchain_free_env() -> dict[str, str]:
    """A copy of the environment with the directories holding go/node/npx/mvn
    cut out of PATH, to prove the verifier degrades to WARN instead of FAIL
    where a language runner is simply not installed."""
    env = dict(os.environ)
    drop = set()
    for tool in ("go", "node", "npx", "mvn"):
        found = shutil.which(tool)
        if found:
            drop.add(str(Path(found).parent))
    kept = [e for e in env.get("PATH", "").split(os.pathsep) if e and e not in drop]
    env["PATH"] = os.pathsep.join(kept)
    return env


# Stand-in for mvn: this sandbox has no real Maven toolchain, so the junit
# checks below exercise the surefire report contract (DESIGN) against a
# fake binary instead. -Dtest=<Class>#<method> selects the test source under
# src/test/java/**/<Class>.java (cwd is the module dir, like the real run);
# a missing file/method, or GT_FAKE_MVN_NOREPORT, writes no report and exits
# 0, exactly like real surefire 3.2.5 given a method that does not exist.
# Otherwise it checks the "// probe: <relpath> contains <text>" marker line
# in the test source and writes a surefire XML report reflecting the result.
FAKE_MVN = r'''#!/usr/bin/env python3
import os
import re
import sys
from pathlib import Path

argv = sys.argv[1:]
test_arg = next((a for a in argv if a.startswith("-Dtest=")), None)
if test_arg is None:
    sys.exit(0)
simple_class, _, method = test_arg[len("-Dtest="):].partition("#")

cwd = Path.cwd()
matches = sorted(cwd.glob("src/test/java/**/" + simple_class + ".java"))
if not matches:
    sys.exit(0)
text = matches[0].read_text()
if not re.search(r"\bvoid\s+" + re.escape(method) + r"\s*\(", text):
    sys.exit(0)
if os.environ.get("GT_FAKE_MVN_NOREPORT"):
    sys.exit(0)

pkg_m = re.search(r"(?m)^package\s+([\w.]+);", text)
fqn = pkg_m.group(1) + "." + simple_class if pkg_m else simple_class

ok = True
probe_m = re.search(r"(?m)^// probe: (\S+) contains (.+)$", text)
if probe_m:
    rel, needle = probe_m.group(1), probe_m.group(2)
    try:
        ok = needle in (cwd / rel).read_text()
    except OSError:
        ok = False

report_dir = cwd / "target" / "surefire-reports"
report_dir.mkdir(parents=True, exist_ok=True)
failure = "" if ok else '<failure message="probe mismatch"/>'
report = report_dir / ("TEST-" + fqn + ".xml")
report.write_text(
    '<testsuite name="' + fqn + '"><testcase name="' + method + '" classname="' + fqn + '">'
    + failure + "</testcase></testsuite>"
)
sys.exit(0 if ok else 1)
'''

FAKE_MVN_BIN = Path(tempfile.mkdtemp(prefix="gt-fake-mvn-"))
(FAKE_MVN_BIN / "mvn").write_text(FAKE_MVN)
(FAKE_MVN_BIN / "mvn").chmod(0o755)


def junit_env(base_env: dict[str, str] | None = None, noreport: bool = False) -> dict[str, str]:
    """A copy of the environment with the fake mvn above first on PATH."""
    env = dict(base_env if base_env is not None else os.environ)
    env["PATH"] = str(FAKE_MVN_BIN) + os.pathsep + env.get("PATH", "")
    if noreport:
        env["GT_FAKE_MVN_NOREPORT"] = "1"
    else:
        env.pop("GT_FAKE_MVN_NOREPORT", None)
    return env


def build_fixture() -> None:
    if FIXTURE.exists():
        shutil.rmtree(FIXTURE)
    FIXTURE.mkdir(parents=True)

    for rel, content in SOURCES.items():
        path = FIXTURE / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    (FIXTURE / "STATUS.yaml").write_text(STATUS_YAML)

    tools_dir = FIXTURE / "tools" / "ground_truth"
    tools_dir.mkdir(parents=True, exist_ok=True)
    (tools_dir / "probes").mkdir(parents=True, exist_ok=True)
    for name in ("contract_lib.py", "verify.py", "gt_mutation_selfcheck.py", "blast_radius.py"):
        shutil.copy(TEMPLATES / name, tools_dir / name)
    shutil.copy(TEMPLATES / "probes" / "example_probe.py", tools_dir / "probes" / "example_probe.py")

    tests_dir = FIXTURE / "tests"
    shutil.copy(
        TEMPLATES / "status_contract_test.py.template",
        tests_dir / "test_status_contract.py",
    )

    for rel in (
        "tools/ground_truth/verify.py",
        "tools/ground_truth/gt_mutation_selfcheck.py",
        "tools/ground_truth/blast_radius.py",
        "tools/ground_truth/probes/example_probe.py",
        "tools/ground_truth/probes/smoke_probe.py",
    ):
        os.chmod(FIXTURE / rel, 0o755)

    subprocess.run(["git", "init", "-q"], cwd=FIXTURE, check=True)
    subprocess.run(["git", "add", "-A"], cwd=FIXTURE, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "initial fixture"], cwd=FIXTURE, check=True
    )


def happy_path() -> bool:
    ok = True

    cp = verify("sync")
    ok &= record("happy: verify --mode=sync exits 0", cp.returncode == 0, out(cp))

    cp = verify("full")
    ok &= record("happy: verify --mode=full exits 0", cp.returncode == 0, out(cp))

    cp = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_status_contract.py", "-q"],
        cwd=FIXTURE,
        capture_output=True,
        text=True,
        timeout=120,
    )
    ok &= record("happy: pytest tests/test_status_contract.py passes", cp.returncode == 0, out(cp))

    cp = mutation_selfcheck()
    ok &= record("happy: gt_mutation_selfcheck exits 0", cp.returncode == 0, out(cp))

    cp = run(["tools/ground_truth/blast_radius.py", "services/alpha"], FIXTURE)
    ok &= record(
        "happy: blast_radius.py on a claim path exits 0 and reports claim status",
        cp.returncode == 0 and "status: " in out(cp),
        out(cp),
    )

    return ok


def fault_scenarios() -> bool:
    ok = True
    status_path = FIXTURE / "STATUS.yaml"
    readme_path = FIXTURE / "README.md"
    beta_test_path = FIXTURE / "tests" / "test_beta.py"

    with patched(status_path, ALPHA_BLOCK, ALPHA_BLOCK.replace("test_greet_returns_greeting", "test_does_not_exist")):
        cp = verify("sync")
        ok &= record(
            "1: check points at nonexistent test function -> exit 1",
            cp.returncode == 1 and "test_does_not_exist" in out(cp) and "not found" in out(cp),
            out(cp),
        )

    with added_file(FIXTURE / "services/extra.py", "X = 1\n"):
        cp = verify("sync", extra=["--show-warn"])
        ok &= record(
            "2: new uncovered file under coverage root -> sync WARN with fix hint, exit 0",
            cp.returncode == 0
            and "[WARN]" in out(cp)
            and "extra.py" in out(cp)
            and "not covered by any claim" in out(cp)
            and "fix: add a claim with path: services/extra.py" in out(cp),
            out(cp),
        )
        cp = verify("full")
        ok &= record(
            "2b: same fault in full mode -> exit 1",
            cp.returncode == 1 and "[FAIL]" in out(cp) and "not covered by any claim" in out(cp),
            out(cp),
        )

    with removed_file(FIXTURE / "main.py"):
        cp = verify("sync", extra=["--show-warn"])
        ok &= record(
            "3: claim path points at a deleted file -> sync WARN with 'deleted' hint, exit 0",
            cp.returncode == 0
            and "main.py" in out(cp)
            and "does not exist" in out(cp)
            and "deleted; fix: drop the path" in out(cp),
            out(cp),
        )
        cp = verify("full")
        ok &= record(
            "3b: same fault in full mode -> exit 1",
            cp.returncode == 1 and "main.py" in out(cp) and "does not exist" in out(cp),
            out(cp),
        )

    subprocess.run(["git", "mv", "main.py", "main2.py"], cwd=FIXTURE, check=True)
    try:
        cp = verify("sync", extra=["--show-warn"])
        ok &= record(
            "3c: claim path points at a git-mv rename -> WARN with 'renamed to' hint",
            cp.returncode == 0 and "main.py" in out(cp) and "renamed to main2.py" in out(cp),
            out(cp),
        )
    finally:
        subprocess.run(["git", "mv", "main2.py", "main.py"], cwd=FIXTURE, check=True)

    head_before_3d = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=FIXTURE, capture_output=True, text=True, check=True
    ).stdout.strip()
    subprocess.run(["git", "mv", "main.py", "main2.py"], cwd=FIXTURE, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "rename main.py to main2.py"], cwd=FIXTURE, check=True)
    (FIXTURE / "README.md").unlink()
    subprocess.run(["git", "commit", "-q", "-am", "delete README.md"], cwd=FIXTURE, check=True)
    try:
        cp = verify("sync", extra=["--show-warn"])
        ok &= record(
            "3d: claim path points at a committed rename -> WARN with 'renamed to' hint",
            cp.returncode == 0 and "main.py" in out(cp) and "renamed to main2.py" in out(cp),
            out(cp),
        )
        cp = verify("full")
        ok &= record(
            "3d2: same fault in full mode -> exit 1 (path missing)",
            cp.returncode == 1 and "main.py" in out(cp) and "does not exist" in out(cp),
            out(cp),
        )
    finally:
        subprocess.run(["git", "reset", "-q", "--hard", head_before_3d], cwd=FIXTURE, check=True)

    with (
        added_file(FIXTURE / "services/extra.py", "X = 1\n"),
        removed_file(FIXTURE / "main.py"),
    ):
        cp = verify("sync")
        warn_lines = [line for line in out(cp).splitlines() if line.startswith("[WARN]")]
        cp_shown = verify("sync", extra=["--show-warn"])
        shown_warn_lines = [line for line in out(cp_shown).splitlines() if line.startswith("[WARN]")]
        ok &= record(
            "g: sync hides individual WARN lines behind one summary",
            cp.returncode == 0
            and len(warn_lines) == 1
            and f"{len(shown_warn_lines)} warnings hidden in sync mode" in warn_lines[0]
            and "--show-warn" in warn_lines[0],
            out(cp),
        )

        ok &= record(
            "g2: --show-warn lists every WARN instead of the summary",
            cp_shown.returncode == 0
            and len(shown_warn_lines) >= 2
            and any(
                "extra.py" in line and "not covered by any claim" in line for line in shown_warn_lines
            )
            and any(
                "main.py" in line and "deleted; fix: drop the path" in line for line in shown_warn_lines
            )
            and all("warnings hidden" not in line for line in shown_warn_lines),
            out(cp_shown),
        )

    with patched(status_path, "    state: open\n", "    state: closed\n"):
        cp = verify("sync")
        ok &= record(
            "c: debt.state 'closed' (outside open/accepted/resolved) -> FAIL in sync",
            cp.returncode == 1
            and "debt.state must be one of" in out(cp)
            and "'closed'" in out(cp),
            out(cp),
        )
        cp = verify("full")
        ok &= record(
            "c2: same fault -> FAIL in full",
            cp.returncode == 1 and "debt.state must be one of" in out(cp),
            out(cp),
        )

    with patched(status_path, '    owner: ""\n', '    owner: ""\n    closed: true\n'):
        cp = verify("sync")
        ok &= record(
            "d: unknown debt key -> FAIL in sync",
            cp.returncode == 1
            and "unknown key 'closed'" in out(cp)
            and "debt_beta_stub" in out(cp),
            out(cp),
        )
        cp = verify("full")
        ok &= record(
            "d2: same fault -> FAIL in full",
            cp.returncode == 1 and "unknown key 'closed'" in out(cp),
            out(cp),
        )

    with patched(status_path, "    state: open\n", "    state: resolved\n"):
        cp = verify("sync")
        ok &= record(
            "4: debt resolved while ref claim is stub -> exit 1",
            cp.returncode == 1 and "still stub" in out(cp),
            out(cp),
        )

    with patched(
        status_path,
        '    probe: "tools/ground_truth/probes/example_probe.py"\n',
        "",
    ):
        cp = verify("sync")
        ok &= record(
            "5: edge without probe -> exit 1",
            cp.returncode == 1 and "edge missing" in out(cp) and "probe" in out(cp),
            out(cp),
        )

    with patched(status_path, 'path: "main.py"', 'path: "docs/_human/notes.md"'):
        cp = verify("sync")
        ok &= record(
            "6: claim path under docs/_human/ -> exit 1",
            cp.returncode == 1 and "docs/_human" in out(cp),
            out(cp),
        )

    with patched(status_path, ALPHA_BLOCK, ALPHA_BLOCK + '    command: "echo hi"\n'):
        cp = verify("sync")
        ok &= record(
            "7: forbidden field for kind (command on status claim) -> exit 1",
            cp.returncode == 1 and "not allowed for kind=status" in out(cp) and "command" in out(cp),
            out(cp),
        )

    with patched(status_path, ALPHA_BLOCK, ALPHA_BLOCK.replace("status: implemented", "status: wip")):
        cp = verify("sync")
        ok &= record(
            "8: unknown status value -> exit 1",
            cp.returncode == 1 and "status must be one of" in out(cp),
            out(cp),
        )

    with patched(status_path, "  - id: handlers_http\n", "  - id: alpha_greeting\n"):
        cp = verify("sync")
        ok &= record(
            "9: duplicate claim id -> exit 1",
            cp.returncode == 1 and "duplicate claim id" in out(cp),
            out(cp),
        )

    fault_10_line = "\nCo-Authored-By: Claude <noreply@example.invalid>\n"  # gt-allow: fault-injection payload for D16 selftest
    with appended(readme_path, fault_10_line):
        cp_sync = verify("sync")
        cp_full = verify("full")
        marker = "Co-Authored-By"  # gt-allow: expected match text for the D16 selftest assertion
        ok &= record(
            "10: identity trailer fails only in full mode",
            cp_sync.returncode == 0 and cp_full.returncode == 1 and marker in out(cp_full),
            f"sync={cp_sync.returncode} full={cp_full.returncode} full_out={out(cp_full)}",
        )

    with patched(status_path, ALPHA_BLOCK, ALPHA_BLOCK.replace("test_greet_returns_greeting", "test_unrelated_passes")):
        cp = mutation_selfcheck("alpha_greeting")
        ok &= record(
            "11: canary check does not depend on mutated constant -> exit 1",
            cp.returncode == 1 and "stayed green" in out(cp),
            out(cp),
        )

    with patched(status_path, ALPHA_BLOCK, ALPHA_BLOCK + "    depends_on:\n      - nonexistent_claim_id\n"):
        cp = verify("sync")
        ok &= record(
            "12: depends_on points at unknown id -> exit 1",
            cp.returncode == 1 and "references unknown claim" in out(cp) and "nonexistent_claim_id" in out(cp),
            out(cp),
        )

    leverage_line = "\nWe should leverage this component more.\n"  # gt-allow: expected match text for the D16 selftest assertion
    with appended(readme_path, leverage_line):  # gt-allow: expected match text for the D16 selftest assertion
        cp = verify("full")
        ok &= record(
            "13: buzzword leverage -> WARN only, exit 0",  # gt-allow: expected match text
            cp.returncode == 0 and "[WARN]" in out(cp) and "leverage" in out(cp),  # gt-allow: expected match text
            out(cp),
        )

    bypass_claim = (
        "  - id: bypass_root_claim\n"
        '    component: "root bypass probe"\n'
        "    kind: status\n"
        "    status: implemented\n"
        '    path: "."\n'
        "    check_kind: pytest\n"
        '    check: "tests/test_alpha.py::test_greet_returns_greeting"\n'
        '    note: "attempted coverage bypass via path: \'.\'"\n'
    )
    with patched(status_path, "claims:\n", "claims:\n" + bypass_claim):
        cp = verify("sync")
        ok &= record(
            "14: claim path '.' cannot swallow the whole coverage scan -> exit 1",
            cp.returncode == 1 and "resolves to the repo root itself" in out(cp),
            out(cp),
        )

    with patched(status_path, "    exclude: []\n", '    exclude: ["**"]\n'):
        cp = verify("sync")
        ok &= record(
            "15: bare '**' exclude is rejected outright -> exit 1",
            cp.returncode == 1 and "bare wildcard" in out(cp),
            out(cp),
        )

    with patched(status_path, 'path: "main.py"', 'path: "handlers/../docs/_human/notes.md"'):
        cp = verify("sync")
        ok &= record(
            "16: docs/_human guard survives a '..' traversal -> exit 1",
            cp.returncode == 1 and "docs/_human" in out(cp),
            out(cp),
        )

    fake_git_dir = SCRATCH_BASE / f"gt-selftest-nogit-{os.getpid()}"
    fake_git_dir.mkdir(exist_ok=True)
    fake_git = fake_git_dir / "git"
    fake_git.write_text("#!/bin/sh\nexit 1\n")
    os.chmod(fake_git, 0o755)
    try:
        broken_env = dict(os.environ)
        broken_env["PATH"] = f"{fake_git_dir}:{broken_env.get('PATH', '')}"
        cp = verify("full", env=broken_env)
        ok &= record(
            "17: git ls-files failure surfaces as FAIL, not a silent clean scan",
            cp.returncode == 1 and "could not enumerate tracked files" in out(cp),
            out(cp),
        )
    finally:
        shutil.rmtree(fake_git_dir, ignore_errors=True)

    with patched(status_path, "    to: handlers_http\n", '    to: "mailer:send_api"\n'):
        cp = verify("sync")
        ok &= record(
            "18: edge.to accepts any '<name>:<text>' external shape, not just 'external-repo:'",
            cp.returncode == 0,
            out(cp),
        )

    with patched(status_path, '    note: "handle() returns a fixed ok response."\n', ""):
        cp = verify("sync")
        ok &= record(
            "19: claim missing note -> exit 1",
            cp.returncode == 1 and "note must be a non-empty string" in out(cp),
            out(cp),
        )

    with patched(status_path, RAISE_FROM, RAISE_TO), appended(beta_test_path, TOUCH_COMMENT):
        cp = verify("full")
        ok &= record(
            "20: open debt (severity normal) whose ref claim is already implemented -> no WARN, exit 0",
            cp.returncode == 0 and "already implemented" not in out(cp),
            out(cp),
        )

    with (
        patched(status_path, RAISE_FROM, RAISE_TO),
        patched(status_path, "    severity: normal\n", "    severity: blocking\n"),
        appended(beta_test_path, TOUCH_COMMENT),
    ):
        cp = verify("full")
        ok &= record(
            "20b: same debt raised to severity blocking -> WARN, exit 0",
            cp.returncode == 0 and "[WARN]" in out(cp) and "already implemented" in out(cp),
            out(cp),
        )

    with patched(status_path, RAISE_FROM, RAISE_TO):
        cp = verify("sync")
        ok &= record(
            "21: status raised with the check target untouched -> exit 1",
            cp.returncode == 1 and "status raised" in out(cp) and "beta_worker_stub" in out(cp),
            out(cp),
        )

    with patched(status_path, RAISE_FROM, RAISE_TO), appended(beta_test_path, TOUCH_COMMENT):
        cp = verify("sync")
        ok &= record(
            "22: same raise with the check's test file modified -> exit 0",
            cp.returncode == 0 and "status raised" not in out(cp),
            out(cp),
        )

    with patched(status_path, BETA_BLOCK, BETA_BLOCK_RECHECKED):
        cp = verify("sync")
        ok &= record(
            "23: status raised together with a new check string -> no status-raise FAIL",
            cp.returncode == 0 and "status raised" not in out(cp),
            out(cp),
        )

    base_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=FIXTURE, capture_output=True, text=True, check=True
    ).stdout.strip()
    original_status = status_path.read_text()
    try:
        status_path.write_text(original_status.replace(RAISE_FROM, RAISE_TO, 1))
        subprocess.run(["git", "commit", "-q", "-a", "-m", "raise beta status"], cwd=FIXTURE, check=True)
        env = dict(os.environ)
        env["GT_BASE_REF"] = base_sha
        cp = verify("full", env=env)
        ok &= record(
            "24: full mode against GT_BASE_REF, raise committed with the test untouched -> exit 1",
            cp.returncode == 1 and "status raised" in out(cp),
            out(cp),
        )
    finally:
        subprocess.run(["git", "reset", "--hard", "-q", base_sha], cwd=FIXTURE, check=True)

    env = {k: v for k, v in os.environ.items() if k != "GT_BASE_REF"}
    cp = verify("full", env=env)
    ok &= record(
        "25: full mode without GT_BASE_REF in a single-commit repo -> exit 0 with a skip WARN",
        cp.returncode == 0 and "status-raise guard skipped" in out(cp),
        out(cp),
    )

    subprocess.run(["git", "rm", "--cached", "-q", "STATUS.yaml"], cwd=FIXTURE, check=True)
    try:
        with patched(status_path, RAISE_FROM, RAISE_TO):
            cp = verify("sync")
            ok &= record(
                "26: untracked STATUS.yaml -> guard stays silent",
                cp.returncode == 0 and "status raised" not in out(cp),
                out(cp),
            )
    finally:
        subprocess.run(["git", "reset", "-q", "HEAD", "--", "STATUS.yaml"], cwd=FIXTURE, check=True)

    original_status = status_path.read_text()
    try:
        status_path.write_text(original_status.replace(RAISE_FROM, RAISE_TO, 1))
        subprocess.run(["git", "commit", "-q", "-a", "-m", "raise beta status"], cwd=FIXTURE, check=True)
        env = dict(os.environ)
        env["GT_BASE_REF"] = "0" * 40
        cp = verify("full", env=env)
        ok &= record(
            "27: unresolvable GT_BASE_REF warns instead of falling back to HEAD~1 -> exit 0",
            cp.returncode == 0
            and "does not resolve" in out(cp)
            and "status raised" not in out(cp),
            out(cp),
        )
    finally:
        subprocess.run(["git", "reset", "--hard", "-q", base_sha], cwd=FIXTURE, check=True)

    mode_before = beta_test_path.stat().st_mode
    try:
        with patched(status_path, RAISE_FROM, RAISE_TO):
            os.chmod(beta_test_path, 0o755)
            cp = verify("sync")
            ok &= record(
                "28: chmod-only touch of the check target does not excuse a raise -> exit 1",
                cp.returncode == 1 and "status raised" in out(cp),
                out(cp),
            )
    finally:
        os.chmod(beta_test_path, mode_before)

    try:
        status_path.write_text(
            original_status.split("claims:", 1)[0] + "claims: null\n"
        )
        subprocess.run(["git", "commit", "-q", "-a", "-m", "baseline without claims"], cwd=FIXTURE, check=True)
        null_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=FIXTURE, capture_output=True, text=True, check=True
        ).stdout.strip()
        status_path.write_text(original_status.replace(RAISE_FROM, RAISE_TO, 1))
        subprocess.run(["git", "commit", "-q", "-a", "-m", "raise beta status"], cwd=FIXTURE, check=True)
        env = dict(os.environ)
        env["GT_BASE_REF"] = null_sha
        cp = verify("full", env=env)
        ok &= record(
            "29: baseline with an unusable claims list warns instead of passing silently",
            cp.returncode == 0 and "does not parse" in out(cp),
            out(cp),
        )
    finally:
        subprocess.run(["git", "reset", "--hard", "-q", base_sha], cwd=FIXTURE, check=True)

    with patched(status_path, "    state: open\n", "    state: resolved\n"):
        cp = verify("sync")
        ok &= record(
            "30: debt resolved with claim's check and target untouched since baseline -> exit 1",
            cp.returncode == 1
            and "resolved without proof" in out(cp)
            and "debt_beta_stub" in out(cp),
            out(cp),
        )

    try:
        status_path.write_text(original_status.replace("    state: open\n", "    state: resolved\n", 1))
        subprocess.run(["git", "commit", "-q", "-a", "-m", "resolve beta debt"], cwd=FIXTURE, check=True)
        env = dict(os.environ)
        env["GT_BASE_REF"] = base_sha
        cp = verify("full", env=env)
        ok &= record(
            "31: same debt resolve committed against GT_BASE_REF -> exit 1",
            cp.returncode == 1 and "resolved without proof" in out(cp),
            out(cp),
        )
    finally:
        subprocess.run(["git", "reset", "--hard", "-q", base_sha], cwd=FIXTURE, check=True)

    with (
        patched(status_path, RAISE_FROM, RAISE_TO),
        patched(status_path, "    state: open\n", "    state: resolved\n"),
        appended(beta_test_path, TOUCH_COMMENT),
    ):
        cp = verify("sync")
        ok &= record(
            "32: same debt resolve with the claim raised and its test touched -> exit 0",
            cp.returncode == 0 and "resolved without proof" not in out(cp),
            out(cp),
        )

    yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
    tomorrow = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()
    backfilled_debt = (
        "  - id: debt_beta_backfilled\n"
        "    ref: beta_worker_stub\n"
        '    description: "resolved by a commit that already landed"\n'
        "    severity: normal\n"
        "    state: resolved\n"
        '    owner: ""\n'
        f'    opened: "{yesterday}"\n'
    )
    try:
        beta_test_path.write_text(beta_test_path.read_text() + TOUCH_COMMENT)
        status_path.write_text(original_status.replace(RAISE_FROM, RAISE_TO, 1))
        subprocess.run(["git", "commit", "-q", "-a", "-m", "touch beta test"], cwd=FIXTURE, check=True)
        touch_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=FIXTURE, capture_output=True, text=True, check=True
        ).stdout.strip()
        status_path.write_text(status_path.read_text() + backfilled_debt)
        subprocess.run(["git", "commit", "-q", "-a", "-m", "resolve backfilled debt"], cwd=FIXTURE, check=True)
        env = dict(os.environ)
        env["GT_BASE_REF"] = touch_sha
        cp = verify("full", env=env)
        ok &= record(
            "32b: debt resolved by a fix committed before this range -> WARN, exit 0",
            cp.returncode == 0
            and "[WARN]" in out(cp)
            and "debt_beta_backfilled" in out(cp)
            and "resolved without a change in this range" in out(cp)
            and "targets last touched" in out(cp),
            out(cp),
        )
    finally:
        subprocess.run(["git", "reset", "--hard", "-q", base_sha], cwd=FIXTURE, check=True)

    future_debt = (
        "  - id: debt_beta_unresolved\n"
        "    ref: beta_worker_stub\n"
        '    description: "resolved without proof anywhere"\n'
        "    severity: normal\n"
        "    state: resolved\n"
        '    owner: ""\n'
        f'    opened: "{tomorrow}"\n'
    )
    try:
        status_path.write_text(status_path.read_text() + future_debt)
        subprocess.run(["git", "commit", "-q", "-a", "-m", "resolve future debt"], cwd=FIXTURE, check=True)
        env = dict(os.environ)
        env["GT_BASE_REF"] = base_sha
        cp = verify("full", env=env)
        ok &= record(
            "32c: debt with opened in the future and target never touched -> FAIL, exit 1",
            cp.returncode == 1
            and "[FAIL]" in out(cp)
            and "debt_beta_unresolved" in out(cp)
            and "resolved without proof" in out(cp),
            out(cp),
        )
    finally:
        subprocess.run(["git", "reset", "--hard", "-q", base_sha], cwd=FIXTURE, check=True)

    gamma_claim = (
        "  - id: gamma_dashboard_path\n"
        '    component: "ops"\n'
        "    kind: out_of_repo\n"
        '    pointer: "https://example.invalid/gamma-dashboard"\n'
        '    path: "README.md"\n'
        '    note: "gamma dashboard outside the repo; README.md documents it."\n'
    )
    gamma_debt_open = (
        "  - id: debt_gamma_dashboard\n"
        "    ref: gamma_dashboard_path\n"
        '    description: "gamma dashboard debt, proof lives outside the check field"\n'
        "    severity: normal\n"
        "    state: open\n"
        '    owner: ""\n'
        '    opened: "2020-01-01"\n'
    )
    try:
        with_claim = original_status.replace("claims:\n", "claims:\n" + gamma_claim, 1)
        status_path.write_text(with_claim.replace("debt:\n", "debt:\n" + gamma_debt_open, 1))
        subprocess.run(["git", "commit", "-q", "-a", "-m", "add gamma dashboard debt"], cwd=FIXTURE, check=True)
        gamma_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=FIXTURE, capture_output=True, text=True, check=True
        ).stdout.strip()

        with (
            patched(status_path, "    state: open\n", "    state: resolved\n"),
            appended(readme_path, "\ntouched alongside the claim it proves\n"),
        ):
            env = dict(os.environ)
            env["GT_BASE_REF"] = gamma_sha
            cp = verify("full", env=env)
            ok &= record(
                "32d: out_of_repo debt resolved via claim.path fallback, target touched -> exit 0",
                cp.returncode == 0
                and "resolved without proof" not in out(cp)
                and "resolved without a change in this range" not in out(cp),
                out(cp),
            )

        status_path.write_text(
            status_path.read_text().replace("    state: open\n", "    state: resolved\n", 1)
        )
        subprocess.run(["git", "commit", "-q", "-a", "-m", "resolve gamma dashboard debt"], cwd=FIXTURE, check=True)
        env = dict(os.environ)
        env["GT_BASE_REF"] = gamma_sha
        cp = verify("full", env=env)
        ok &= record(
            "32e: same debt resolved by commit, claim.path target untouched in range -> WARN, exit 0",
            cp.returncode == 0
            and "[WARN]" in out(cp)
            and "debt_gamma_dashboard" in out(cp)
            and "resolved without a change in this range" in out(cp),
            out(cp),
        )
    finally:
        subprocess.run(["git", "reset", "--hard", "-q", base_sha], cwd=FIXTURE, check=True)

    no_target_debt = (
        "  - id: debt_external_dashboard\n"
        "    ref: external_dashboard\n"
        '    description: "dashboard claim closed without naming what changed"\n'
        "    severity: normal\n"
        "    state: resolved\n"
        '    owner: ""\n'
        f'    opened: "{yesterday}"\n'
    )
    with patched(status_path, "debt:\n", "debt:\n" + no_target_debt):
        env = dict(os.environ)
        env["GT_BASE_REF"] = base_sha
        cp = verify("full", env=env)
        ok &= record(
            "32f: ref claim with neither check nor path -> WARN, exit 0",
            cp.returncode == 0
            and "has no check or path to verify against" in out(cp)
            and "check unchanged and targets untouched" not in out(cp),
            out(cp),
        )
        cp = verify("sync", extra=["--show-warn"])
        ok &= record(
            "32f-sync: same, sync mode with --show-warn also reports it",
            cp.returncode == 0 and "has no check or path to verify against" in out(cp),
            out(cp),
        )

    return ok


def banned_words_exclude_scenarios() -> bool:
    """Prove meta.banned_words.exclude actually skips a matching file (with
    exactly one WARN naming the entry), that an entry matching nothing still
    gets its own WARN, and that check_shape rejects a malformed entry or one
    that would cover a contract artifact (tests/**)."""
    ok = True
    status_path = FIXTURE / "STATUS.yaml"
    readme_path = FIXTURE / "README.md"

    exclude_readme_block = (
        "    runner: pytest\n"
        "  banned_words:\n"
        "    exclude:\n"
        '      - path: "README.md"\n'
        '        why: "selftest fixture: proves the exclude skip path"\n'
    )
    identity_line = "\nClaude wrote this note.\n"  # gt-allow: fault-injection payload for the exclude selftest

    with patched(status_path, "    runner: pytest\n", exclude_readme_block):
        with appended(readme_path, identity_line):
            cp = verify("full")
            ok &= record(
                "30: banned_words.exclude skips a matching file, WARN instead of FAIL",
                cp.returncode == 0
                and "banned word" not in out(cp)
                and "banned-word scan skipped 1 file(s) matching 'README.md'" in out(cp),
                out(cp),
            )

    missing_why_block = (
        "    runner: pytest\n"
        "  banned_words:\n"
        "    exclude:\n"
        '      - path: "README.md"\n'
    )
    with patched(status_path, "    runner: pytest\n", missing_why_block):
        cp = verify("sync")
        ok &= record(
            "31: banned_words.exclude entry without 'why' -> exit 1",
            cp.returncode == 1 and "must have a non-empty string 'why'" in out(cp),
            out(cp),
        )

    tests_exclude_block = (
        "    runner: pytest\n"
        "  banned_words:\n"
        "    exclude:\n"
        '      - path: "tests/*"\n'
        '        why: "trying to exclude protected contract artifacts"\n'
    )
    with patched(status_path, "    runner: pytest\n", tests_exclude_block):
        cp = verify("sync")
        ok &= record(
            "32: banned_words.exclude may not cover tests/** -> exit 1",
            cp.returncode == 1 and "may not cover contract artifacts" in out(cp),
            out(cp),
        )

    nomatch_exclude_block = (
        "    runner: pytest\n"
        "  banned_words:\n"
        "    exclude:\n"
        '      - path: "no/such/path/*.zzz"\n'
        '        why: "proves the no-match WARN"\n'
    )
    with patched(status_path, "    runner: pytest\n", nomatch_exclude_block):
        cp = verify("full")
        ok &= record(
            "33: banned_words.exclude entry matching no tracked file -> WARN, exit 0",
            cp.returncode == 0 and "matched no tracked file" in out(cp),
            out(cp),
        )

    new_note_path = FIXTURE / "docs" / "new_note.md"
    new_note_content = "# Notes\n\nClaude helped draft this note.\n"  # gt-allow: fault-injection payload for the untracked-file D16 selftest (T2/D36)
    with added_file(new_note_path, new_note_content):
        cp_sync = verify("sync")
        cp_full = verify("full")
        ok &= record(
            "34: untracked file with a banned word is caught in full mode, sync untouched",
            cp_sync.returncode == 0
            and cp_full.returncode == 1
            and "banned word" in out(cp_full)
            and "new_note.md" in out(cp_full),
            f"sync={cp_sync.returncode} full={cp_full.returncode} full_out={out(cp_full)}",
        )

    return ok


def step4_scenarios() -> bool:
    """Guards added in S8 step 4: debt coverage, audit freshness, per-root
    canaries, FAIL for a brand-new claim whose check asserts nothing, the
    path-changed exemption of the status-raise guard, and real execution of
    go/js checks in full mode. Plus two ways a check could quietly cover
    nothing: a coverage root spelled "./x" or ".", and a js test name
    holding regex metacharacters."""
    ok = True
    status_path = FIXTURE / "STATUS.yaml"
    worker_path = FIXTURE / "services" / "beta" / "worker.py"
    majority_path = FIXTURE / "svc" / "majority.go"
    state_path = FIXTURE / "web" / "state.js"
    original_status = status_path.read_text()
    original_worker = worker_path.read_text()
    base_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=FIXTURE, capture_output=True, text=True, check=True
    ).stdout.strip()

    def restore() -> None:
        subprocess.run(["git", "reset", "--hard", "-q", base_sha], cwd=FIXTURE, check=True)
        subprocess.run(["git", "clean", "-fdq"], cwd=FIXTURE, check=True)

    with patched(status_path, "    ref: beta_worker_stub\n", "    ref: alpha_greeting\n"):
        cp = verify("sync")
        ok &= record(
            "35: stub claim with no open debt entry -> exit 1",
            cp.returncode == 1
            and "beta_worker_stub" in out(cp)
            and "without open/accepted debt" in out(cp),
            out(cp),
        )

    with patched(status_path, "    state: open\n", "    state: accepted\n"):
        cp = verify("sync")
        ok &= record(
            "36: accepted debt satisfies the stub claim -> exit 0",
            cp.returncode == 0 and "without open/accepted debt" not in out(cp),
            out(cp),
        )

    with patched(status_path, f'last_audited: "{TODAY}"', f'last_audited: "{OLD_AUDIT_DATE}"'):
        cp_sync = verify("sync", extra=["--show-warn"])
        cp_full = verify("full")
        ok &= record(
            "37: audit older than the day limit -> WARN in sync and full",
            cp_sync.returncode == 0
            and "[WARN]" in out(cp_sync)
            and "run the ground-truth audit" in out(cp_sync)
            and cp_full.returncode == 0
            and "run the ground-truth audit" in out(cp_full),
            f"sync={cp_sync.returncode} full={cp_full.returncode} out={out(cp_full)}",
        )

    cp = verify("sync", extra=["--show-warn"])
    ok &= record(
        "38: audit dated today is not due",
        cp.returncode == 0 and "audit due" not in out(cp),
        out(cp),
    )

    ok &= record(
        "39: meta.last_audited_commit absent -> WARN only, never FAIL",
        cp.returncode == 0 and "last_audited_commit missing" in out(cp),
        out(cp),
    )

    commit_line = f'  last_audited_commit: "{base_sha}"\n'
    with patched(status_path, f'  last_audited: "{TODAY}"\n', f'  last_audited: "{TODAY}"\n' + commit_line):
        cp = verify("sync")
        ok &= record(
            "40: resolvable last_audited_commit with nothing changed since -> exit 0, no WARN",
            cp.returncode == 0
            and "last_audited_commit" not in out(cp)
            and "audit due" not in out(cp),
            out(cp),
        )

    try:
        notes = FIXTURE / "services" / "notes"
        notes.mkdir()
        for i in range(51):
            (notes / f"note_{i:03d}.txt").write_text("x\n")
        status_path.write_text(
            original_status.replace(
                f'  last_audited: "{TODAY}"\n',
                f'  last_audited: "{TODAY}"\n' + commit_line,
                1,
            )
        )
        subprocess.run(["git", "add", "-A"], cwd=FIXTURE, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "bulk notes"], cwd=FIXTURE, check=True)
        cp_sync = verify("sync", extra=["--show-warn"])
        cp_full = verify("full")
        ok &= record(
            "41: more than the file limit changed since last_audited_commit -> WARN sync and full",
            cp_sync.returncode == 0
            and "51 files changed under the coverage roots" in out(cp_sync)
            and cp_full.returncode == 0
            and "run the ground-truth audit" in out(cp_full),
            f"sync={cp_sync.returncode} full={cp_full.returncode} out={out(cp_sync)}",
        )
    finally:
        restore()

    with patched(status_path, HANDLERS_CANARY, ""):
        cp_sync = verify("sync", extra=["--show-warn"])
        cp_full = verify("full")
        ok &= record(
            "42: coverage root with implemented claims and no canary -> WARN sync, FAIL full",
            cp_sync.returncode == 0
            and "coverage root handlers" in out(cp_sync)
            and "no canary" in out(cp_sync)
            and cp_full.returncode == 1
            and "no canary" in out(cp_full),
            f"sync={cp_sync.returncode} full={cp_full.returncode} out={out(cp_sync)}",
        )

    with (
        patched(status_path, HANDLERS_CANARY, ""),
        patched(status_path, HANDLERS_BLOCK, HANDLERS_BLOCK.replace("implemented", "design-only")),
    ):
        cp = verify("sync")
        ok &= record(
            "43: coverage root whose claims are all design-only needs no canary -> exit 0",
            cp.returncode == 0 and "no canary" not in out(cp),
            out(cp),
        )

    with patched(status_path, "claims:\n", "claims:\n" + ZERO_ASSERTION_CLAIM):
        cp = verify("sync")
        ok &= record(
            "44: claim new in this change whose check asserts nothing -> exit 1",
            cp.returncode == 1
            and "alpha_records_nothing" in out(cp)
            and "no assertions" in out(cp),
            out(cp),
        )

    try:
        status_path.write_text(
            original_status.replace("claims:\n", "claims:\n" + ZERO_ASSERTION_CLAIM, 1)
        )
        subprocess.run(["git", "commit", "-q", "-a", "-m", "add claim"], cwd=FIXTURE, check=True)
        cp = verify("sync", extra=["--show-warn"])
        ok &= record(
            "45: same zero-assertion claim already in the baseline -> WARN, exit 0",
            cp.returncode == 0
            and "[WARN]" in out(cp)
            and "alpha_records_nothing" in out(cp)
            and "no assertions" in out(cp),
            out(cp),
        )
    finally:
        restore()

    with patched(status_path, RAISE_FROM, RAISE_TO), appended(worker_path, WORKER_TOUCH):
        cp = verify("sync")
        ok &= record(
            "46: status raised with the claim's own code changed -> exit 0 in sync",
            cp.returncode == 0 and "status raised" not in out(cp),
            out(cp),
        )

    try:
        status_path.write_text(original_status.replace(RAISE_FROM, RAISE_TO, 1))
        worker_path.write_text(original_worker + WORKER_TOUCH)
        subprocess.run(["git", "commit", "-q", "-a", "-m", "raise with code change"], cwd=FIXTURE, check=True)
        env = dict(os.environ)
        env["GT_BASE_REF"] = base_sha
        cp = verify("full", env=env)
        ok &= record(
            "47: same raise in full mode with the check green -> exit 0",
            cp.returncode == 0 and "status raised" not in out(cp),
            out(cp),
        )
    finally:
        restore()

    try:
        status_path.write_text(original_status.replace(RAISE_FROM, RAISE_TO, 1))
        worker_path.write_text(WORKER_RED)
        subprocess.run(["git", "commit", "-q", "-a", "-m", "raise with red check"], cwd=FIXTURE, check=True)
        env = dict(os.environ)
        env["GT_BASE_REF"] = base_sha
        cp = verify("full", env=env)
        ok &= record(
            "48: same raise in full mode with the check red -> exit 1",
            cp.returncode == 1 and "status raised, check untouched and red" in out(cp),
            out(cp),
        )
    finally:
        restore()

    if shutil.which("go") is None:
        ok &= record("49: go check scenarios skipped (no go toolchain here)", True)
    else:
        with patched(majority_path, 'return "yes"', 'return "no"'):
            cp = verify("full")
            ok &= record(
                "49: go_test claim whose test is red -> exit 1 in full",
                cp.returncode == 1 and "svc_majority" in out(cp) and "check is red" in out(cp),
                out(cp),
            )

    if shutil.which("node") is None:
        ok &= record("50: js check scenarios skipped (no node here)", True)
    else:
        with patched(state_path, 'return "green"', 'return "blue"'):
            cp = verify("full")
            ok &= record(
                "50: js_test claim whose test is red -> exit 1 in full",
                cp.returncode == 1 and "web_state_color" in out(cp) and "check is red" in out(cp),
                out(cp),
            )

    stripped = toolchain_free_env()
    if shutil.which("git", path=stripped["PATH"]) is None:
        ok &= record("51: toolchain-hidden run skipped (git shares the toolchain directory)", True)
    else:
        cp = verify("full", env=stripped)
        ok &= record(
            "51: go/js checks with the toolchains off PATH -> unverified, exit 1",
            cp.returncode == 1 and "check not verified" in out(cp),
            out(cp),
        )

    cp = mutation_selfcheck("svc_majority")
    ok &= record(
        "52: mutation selfcheck drives a go canary",
        cp.returncode == 0 and ("went red" in out(cp) or "[SKIP]" in out(cp)),
        out(cp),
    )

    if shutil.which("go") is not None:
        args = ["tools/ground_truth/gt_mutation_selfcheck.py", "--id", "svc_majority"]
        cp_skip = run(args, FIXTURE, env=stripped)
        cp_strict = run([*args, "--strict"], FIXTURE, env=stripped)
        ok &= record(
            "53: go canary without the toolchain -> SKIP, exit 0; --strict turns it into a failure",
            cp_skip.returncode == 0
            and "[SKIP]" in out(cp_skip)
            and cp_strict.returncode == 1,
            f"skip={cp_skip.returncode} strict={cp_strict.returncode} out={out(cp_skip)}",
        )

    orphan_path = FIXTURE / "services" / "orphan.py"
    with added_file(orphan_path, "def orphan() -> None:\n    return None\n"):
        cp_plain = verify("full")
        with patched(status_path, "      - services\n", "      - ./services\n"):
            cp_dot_slash = verify("full")
        with patched(status_path, "      - services\n", "      - .\n"):
            cp_dot = verify("full")
    ok &= record(
        "54: coverage root written as ./services or . covers the same files as services",
        all(
            cp.returncode == 1 and "services/orphan.py is not covered" in out(cp)
            for cp in (cp_plain, cp_dot_slash, cp_dot)
        ),
        f"plain={cp_plain.returncode} dot_slash={cp_dot_slash.returncode} dot={cp_dot.returncode} "
        f"out={out(cp_dot_slash)}",
    )

    if shutil.which("node") is None:
        ok &= record("55: js name-pattern scenario skipped (no node here)", True)
    else:
        js_test_path = FIXTURE / "web" / "test_state.js"
        with (
            patched(js_test_path, '"yes is green"', '"colorFor(yes) is green"'),
            patched(status_path, "web/test_state.js::yes is green",
                    "web/test_state.js::colorFor(yes) is green"),
            patched(state_path, 'return "green"', 'return "blue"'),
        ):
            cp = verify("full")
            ok &= record(
                "55: js test name holding regex metacharacters still selects the test -> red check caught",
                cp.returncode == 1 and "web_state_color" in out(cp) and "check is red" in out(cp),
                out(cp),
            )

    with patched(status_path, "      - services\n", "      - .\n"):
        cp = verify("sync")
    ok &= record(
        "60: coverage root . does not flag the contract's own tools/ground_truth tree",
        not re.search(r"tools/ground_truth/\S+ is not covered", out(cp)),
        out(cp),
    )

    return ok


def edge_case_scenarios() -> bool:
    """A canary parked on a stub claim, a coverage root reached through a
    symlink, and an audit_close over a last_audited_commit line that carries
    a trailing comment -- three ways a check passed while covering nothing."""
    ok = True
    status_path = FIXTURE / "STATUS.yaml"

    with (
        patched(status_path, HANDLERS_CANARY, ""),
        patched(status_path, "claims:\n", "claims:\n" + HANDLERS_STUB_CANARY_CLAIM),
        appended(status_path, HANDLERS_STUB_DEBT),
    ):
        cp_sync = verify("sync", extra=["--show-warn"])
        cp_full = verify("full")
        ok &= record(
            "56: canary on a stub claim does not satisfy its root -> WARN sync, FAIL full",
            cp_sync.returncode == 0
            and "coverage root handlers" in out(cp_sync)
            and "no canary" in out(cp_sync)
            and cp_full.returncode == 1
            and "no canary" in out(cp_full),
            f"sync={cp_sync.returncode} full={cp_full.returncode} out={out(cp_sync)}",
        )

    sym_repo = SCRATCH_BASE / "symlink-root"
    try:
        (sym_repo / "real" / "src").mkdir(parents=True)
        (sym_repo / "tests").mkdir()
        (sym_repo / "real" / "src" / "covered.py").write_text('def f():\n    return "hi"\n')
        (sym_repo / "real" / "src" / "uncovered.py").write_text("X = 1\n")
        (sym_repo / "tests" / "test_c.py").write_text(
            "from src.covered import f\n\n\ndef test_f():\n    assert f() == \"hi\"\n"
        )
        (sym_repo / "src").symlink_to(Path("real") / "src")
        sym_tools = sym_repo / "tools" / "ground_truth"
        sym_tools.mkdir(parents=True)
        for name in ("contract_lib.py", "verify.py"):
            shutil.copy(TEMPLATES / name, sym_tools / name)
        (sym_repo / "STATUS.yaml").write_text(SYMLINK_STATUS_YAML)
        subprocess.run(["git", "init", "-q"], cwd=sym_repo, check=True)
        subprocess.run(["git", "add", "-A"], cwd=sym_repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "symlinked root"], cwd=sym_repo, check=True)
        cp = run(["tools/ground_truth/verify.py", "--mode=full"], sym_repo)
        ok &= record(
            "57: file under a symlinked coverage root that no claim covers -> exit 1",
            cp.returncode == 1 and "real/src/uncovered.py is not covered by any claim" in out(cp),
            out(cp),
        )
    finally:
        shutil.rmtree(sym_repo, ignore_errors=True)

    close_repo = SCRATCH_BASE / "audit-close"
    close_research = SCRATCH_BASE / "audit-close-research"
    try:
        shutil.copytree(FIXTURE, close_repo, ignore=shutil.ignore_patterns(".git"))
        close_research.mkdir()
        close_status = (close_repo / "STATUS.yaml").read_text()
        close_status = close_status.replace(
            f'  last_audited: "{TODAY}"',
            f'  last_audited: "{TODAY}"\n'
            '  # last_audited_commit: commented example\n'
            '  last_audited_commit: "9f2c1ab"  # optional')
        (close_repo / "STATUS.yaml").write_text(close_status)
        subprocess.run(["git", "init", "-q"], cwd=close_repo, check=True)
        subprocess.run(["git", "add", "-A"], cwd=close_repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "contract only"], cwd=close_repo, check=True)
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=close_repo, capture_output=True, text=True, check=True
        ).stdout.strip()
        run_json = SCRATCH_BASE / "audit-close-run.json"
        run_json.write_text(
            json.dumps(
                {
                    "repo": str(close_repo),
                    "research": str(close_research),
                    "scratch": str(SCRATCH_BASE / "audit-close-scratch"),
                    "python": sys.executable,
                    "skill": str(SKILL_ROOT),
                    "date": TODAY,
                }
            )
        )
        cp = run(
            [
                str(SKILL_ROOT / "pipeline" / "gt.py"),
                "audit_close",
                "--run",
                str(run_json),
                "--write",
                "--force",
                "--reviewed",
            ],
            close_repo,
        )
        import yaml  # noqa: E402

        text = (close_repo / "STATUS.yaml").read_text()
        live = [ln for ln in text.splitlines() if ln.lstrip().startswith("last_audited_commit:")]
        ok &= record(
            "58: audit_close replaces a last_audited_commit line that carries a comment",
            cp.returncode == 0
            and len(live) == 1
            and "# optional" in live[0]
            and (yaml.safe_load(text).get("meta") or {}).get("last_audited_commit") == head,
            f"rc={cp.returncode} lines={live} out={out(cp)}",
        )
    finally:
        shutil.rmtree(close_repo, ignore_errors=True)
        shutil.rmtree(close_research, ignore_errors=True)

    # 59: a settings file written before the template grew its trailing
    # "# gt-allow" comment must not receive a second copy of the same hook.
    sys.path.insert(0, str(SKILL_ROOT / "pipeline" / "stages"))
    import i9_install  # noqa: E402

    addition = i9_install._settings_addition(TEMPLATES, ".claude/hooks")
    bare = {
        "hooks": {
            "Stop": [{"hooks": [{"type": "command", "command": "bash .claude/hooks/ground-truth-sync-check.sh"}]}],
        }
    }
    merged, _changed = i9_install.merge_settings(bare, addition)
    stop_cmds = [h["command"] for g in merged["hooks"]["Stop"] for h in g["hooks"] if "ground-truth" in h["command"]]
    ok &= record(
        "59: merge_settings treats a hook command with and without its trailing comment as one hook",
        len(stop_cmds) == 1 and len(merged["hooks"].get("PreToolUse", [])) == 1,
        f"stop={stop_cmds} events={list(merged['hooks'])}",
    )

    # 59b: the template raised the edit guard's timeout after a repository
    # was first installed -- the merge must carry the raise into the
    # existing entry (nothing else in the file changes) and never lower a
    # timeout somebody raised by hand.
    stale = {
        "hooks": {
            "PreToolUse": [{"matcher": "Edit|Write", "hooks": [{"type": "command", "command": "bash .claude/hooks/ground-truth-edit-guard.sh", "timeout": 10}]}],
            "Stop": [{"hooks": [{"type": "command", "command": "bash .claude/hooks/ground-truth-sync-check.sh", "timeout": 900}]}],
        }
    }
    merged, changed = i9_install.merge_settings(stale, addition)
    guard_t = [h["timeout"] for g in merged["hooks"]["PreToolUse"] for h in g["hooks"]]
    stop_t = [h["timeout"] for g in merged["hooks"]["Stop"] for h in g["hooks"] if "ground-truth" in h["command"]]
    ok &= record(
        "59b: merge_settings raises a present hook's timeout to the template's value and never lowers one",
        changed and guard_t == [30] and stop_t == [900] and len(merged["hooks"]["PreToolUse"]) == 1,
        f"changed={changed} guard={guard_t} stop={stop_t}",
    )

    # 61: a flat repository declares coverage root "." -- every claim path
    # must land in a bucket, otherwise the fleet gets zero targets.
    import i10_build_args  # noqa: E402

    flat_claims = [
        {"id": "a", "kind": "status", "path": "exporter.py"},
        {"id": "b", "kind": "status", "path": ["install.sh", "uninstall.sh"]},
        {"id": "c", "kind": "live_state", "path": "exporter.py"},
    ]
    buckets = i10_build_args.build_file_buckets(flat_claims, ["."])
    ok &= record(
        "61: i10_build_args buckets every status claim path under coverage root .",
        sorted(buckets) == ["exporter.py", "install.sh", "uninstall.sh"] and len(buckets["exporter.py"]) == 1,
        f"buckets={ {k: [c['id'] for c in v] for k, v in buckets.items()} }",
    )

    return ok


# A junit claim (check_kind DESIGN) resolved against a fake Maven module:
# jvm/pom.xml marks the module, fx.Ledger.balance() is the code under test,
# fx.LedgerTest carries the probe marker the fake mvn above reads.
JUNIT_CLAIM = (
    "  - id: ledger_books_balance\n"
    '    component: "ledger"\n'
    "    kind: status\n"
    "    status: implemented\n"
    "    check_kind: junit\n"
    '    check: "fx.LedgerTest::booksBalance"\n'
    '    path: "jvm/src/main/java/fx/Ledger.java"\n'
    '    note: "Ledger.balance() reports balanced; the canary flips the returned string."\n'
    "    canary: true\n"
    "    mutation:\n"
    '      file: "jvm/src/main/java/fx/Ledger.java"\n'
    "      find: 'return \"balanced\"'\n"
    "      replace: 'return \"unbalanced\"'\n"
)

JVM_POM = (
    "<project>\n"
    "  <modelVersion>4.0.0</modelVersion>\n"
    "  <groupId>gtselftest</groupId>\n"
    "  <artifactId>jvm</artifactId>\n"
    "  <version>1.0</version>\n"
    "</project>\n"
)

JVM_LEDGER = (
    "package fx;\n"
    "\n"
    "// Ledger reports whether the books balance.\n"
    "public class Ledger {\n"
    "    public static String balance() {\n"
    '        return "balanced";\n'
    "    }\n"
    "}\n"
)

JVM_LEDGER_TEST = (
    "package fx;\n"
    "\n"
    "import org.junit.jupiter.api.Test;\n"
    "import static org.junit.jupiter.api.Assertions.assertEquals;\n"
    "\n"
    '// probe: src/main/java/fx/Ledger.java contains return "balanced"\n'
    "public class LedgerTest {\n"
    "    @Test\n"
    "    void booksBalance() {\n"
    '        assertEquals("balanced", Ledger.balance());\n'
    "    }\n"
    "}\n"
)


def junit_scenarios() -> bool:
    """The junit check_kind end to end: static resolution, a real run
    through the fake mvn toolchain, and the surefire report contract
    (missing report / missing testcase / stale report) it depends on."""
    ok = True
    status_path = FIXTURE / "STATUS.yaml"
    pom_path = FIXTURE / "jvm" / "pom.xml"
    ledger_path = FIXTURE / "jvm" / "src" / "main" / "java" / "fx" / "Ledger.java"
    ledger_test_path = FIXTURE / "jvm" / "src" / "test" / "java" / "fx" / "LedgerTest.java"
    for p in (pom_path, ledger_path, ledger_test_path):
        p.parent.mkdir(parents=True, exist_ok=True)

    with (
        patched(status_path, "claims:\n", "claims:\n" + JUNIT_CLAIM),
        added_file(pom_path, JVM_POM),
        added_file(ledger_path, JVM_LEDGER),
        added_file(ledger_test_path, JVM_LEDGER_TEST),
    ):
        # mutation_selfcheck refuses to mutate a file with uncommitted
        # changes; the jvm module is added straight to the working tree
        # above, so it has to be committed once before the canary check.
        subprocess.run(["git", "add", "jvm"], cwd=FIXTURE, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "add jvm module for junit selftest"], cwd=FIXTURE, check=True)

        cp = verify("full", junit_env())
        ok &= record(
            "69: junit claim green -> exit 0, no WARN/FAIL for ledger_books_balance",
            cp.returncode == 0 and "ledger_books_balance" not in out(cp),
            out(cp),
        )

        with patched(ledger_path, 'return "balanced"', 'return "unbalanced"'):
            cp = verify("full", junit_env())
            ok &= record(
                "70: junit claim whose test is red -> exit 1 in full",
                cp.returncode != 0 and "ledger_books_balance" in out(cp),
                out(cp),
            )

        with patched(status_path, 'check: "fx.LedgerTest::booksBalance"', 'check: "fx.LedgerTest::nope"'):
            cp = verify("sync", toolchain_free_env())
            ok &= record(
                "71: static phantom method -> sync catches it without mvn",
                cp.returncode != 0 and "junit check not found: fx.LedgerTest::nope" in out(cp),
                out(cp),
            )

        cp = verify("full", junit_env(noreport=True))
        ok &= record(
            "72: runtime phantom -- mvn exits 0 but writes no report -> red, 'did not run'",
            cp.returncode != 0 and "did not run" in out(cp),
            out(cp),
        )

        cp = verify("full", toolchain_free_env())
        ok &= record(
            "73: no mvn on PATH -> unverified 'maven toolchain not found', exit 1",
            cp.returncode == 1 and "maven toolchain not found" in out(cp),
            out(cp),
        )

        cp = mutation_selfcheck("ledger_books_balance", env=junit_env())
        ok &= record(
            "74: mutation selfcheck drives a junit canary",
            cp.returncode == 0 and "went red" in out(cp),
            out(cp),
        )

        report_path = FIXTURE / "jvm" / "target" / "surefire-reports" / "TEST-fx.LedgerTest.xml"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            '<testsuite name="fx.LedgerTest">'
            '<testcase name="booksBalance" classname="fx.LedgerTest"/></testsuite>'
        )
        try:
            cp = verify("full", junit_env(noreport=True))
            ok &= record(
                "75: stale surefire report is deleted before the run, not read as a pass",
                cp.returncode != 0 and "did not run" in out(cp),
                out(cp),
            )
        finally:
            shutil.rmtree(FIXTURE / "jvm" / "target", ignore_errors=True)

    ok &= surefire_parser_scenario()
    ok &= i9_junit_scenario()
    ok &= i9_ci_cache_scenario()
    ok &= i9_nested_python_scenario()
    return ok


I9_MAVEN_POM = (
    "<project>\n"
    "  <modelVersion>4.0.0</modelVersion>\n"
    "  <groupId>gtselftest</groupId>\n"
    "  <artifactId>i9maven</artifactId>\n"
    "  <version>1.0</version>\n"
    "</project>\n"
)

I9_MAVEN_GITLAB_CI = (
    "stages:\n"
    "  - test\n"
    "\n"
    "build:\n"
    "  image: maven:3.8-eclipse-temurin-17\n"
    "  stage: test\n"
    "  script:\n"
    "    - mvn -q compile\n"
)

I9_MAVEN_APP_TEST = "package org.acme.app;\n\nclass AppTest {\n}\n"
I9_MAVEN_OTHER_TEST = "package org.acme.other;\n\nclass OtherTest {\n}\n"


def _first_code_line(text: str) -> str:
    for ln in text.splitlines():
        stripped = ln.strip()
        if stripped and not stripped.startswith("//"):
            return stripped
    return ""


def surefire_parser_scenario() -> bool:
    """77: the _surefire_outcome branches the fake mvn never exercises --
    <failure>/<error> messages, <skipped>, parameterized testcase names."""
    sys.path.insert(0, str(TEMPLATES))
    import contract_lib  # noqa: E402

    fqn = "fx.LedgerTest"

    def report(body: str) -> Path:
        path = Path(tempfile.mkdtemp()) / f"TEST-{fqn}.xml"
        path.write_text(f'<testsuite name="{fqn}">{body}</testsuite>')
        return path

    cases = [
        ("failure message", f'<testcase classname="{fqn}" name="booksBalance"><failure message="expected balanced">trace</failure></testcase>', (False, "expected balanced")),
        ("error message", f'<testcase classname="{fqn}" name="booksBalance"><error message="boom"/></testcase>', (False, "boom")),
        ("skipped", f'<testcase classname="{fqn}" name="booksBalance"><skipped/></testcase>', (False, "test skipped")),
        ("parameterized [1]", f'<testcase classname="{fqn}" name="booksBalance[1]"/>', (True, "")),
        ("parameterized (String)", f'<testcase classname="{fqn}" name="booksBalance(String)"/>', (True, "")),
        ("other class only", '<testcase classname="fx.OtherTest" name="booksBalance"/>', (False, "testcase booksBalance not in surefire report: test did not run")),
    ]
    bad = []
    for label, body, want in cases:
        path = report(body)
        got = contract_lib._surefire_outcome(path, fqn, "booksBalance")
        shutil.rmtree(path.parent, ignore_errors=True)
        if got != want:
            bad.append(f"{label}: got {got!r}, want {want!r}")
    return record(
        "77: _surefire_outcome reads failure/error messages, skipped, and parameterized testcase names",
        not bad,
        "\n".join(bad) or "all 6 report shapes matched",
    )


def i9_junit_scenario() -> bool:
    """i9_install's Java/Maven surface: --facts detects the shallowest
    package, --write installs the bridge test under it and rewrites the
    gitlab CI job for maven, --java-pkg overrides detection, and a
    --java-pkg naming a directory that does not exist is a FAIL."""
    repo = SCRATCH_BASE / "i9-maven-repo"
    repo.mkdir()
    (repo / "pom.xml").write_text(I9_MAVEN_POM)
    (repo / ".gitlab-ci.yml").write_text(I9_MAVEN_GITLAB_CI)
    app_test = repo / "src" / "test" / "java" / "org" / "acme" / "app" / "AppTest.java"
    other_test = repo / "src" / "test" / "java" / "org" / "acme" / "other" / "OtherTest.java"
    app_test.parent.mkdir(parents=True)
    other_test.parent.mkdir(parents=True)
    app_test.write_text(I9_MAVEN_APP_TEST)
    other_test.write_text(I9_MAVEN_OTHER_TEST)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)

    research = SCRATCH_BASE / "i9-maven-research"
    research.mkdir()
    run_json = SCRATCH_BASE / "i9-maven-run.json"
    run_json.write_text(
        json.dumps(
            {
                "repo": str(repo),
                "research": str(research),
                "scratch": str(SCRATCH_BASE / "i9-maven-scratch"),
                "python": sys.executable,
                "skill": str(SKILL_ROOT),
                "date": TODAY,
            }
        )
    )
    base_args = ["pipeline/stages/i9_install.py", "--run", str(run_json)]

    cp_facts = run([*base_args, "--facts"], SKILL_ROOT)
    facts = json.loads(cp_facts.stdout)

    cp_write = run([*base_args, "--write"], SKILL_ROOT)
    dst = app_test.parent / "StatusContractTest.java"
    first_line = _first_code_line(dst.read_text()) if dst.is_file() else ""
    ci_text = (repo / ".gitlab-ci.yml").read_text()

    cp_other = run([*base_args, "--java-pkg", "org.acme.other", "--write"], SKILL_ROOT)
    dst_other = other_test.parent / "StatusContractTest.java"
    first_other = _first_code_line(dst_other.read_text()) if dst_other.is_file() else ""

    cp_bad = run([*base_args, "--java-pkg", "org.nope", "--dry-run"], SKILL_ROOT)

    detail = (
        f"facts.maven={facts.get('maven')} write_rc={cp_write.returncode} first_line={first_line!r} "
        f"other_rc={cp_other.returncode} first_other={first_other!r} bad_rc={cp_bad.returncode} "
        f"bad_out={cp_bad.stdout!r}"
    )
    return record(
        "76: i9 detects the Java package, installs the bridge under it, rewrites the gitlab CI "
        "job for maven, --java-pkg overrides detection, and a nonexistent --java-pkg is a FAIL",
        facts.get("maven", {}).get("package") == "org.acme.app"
        and first_line == "package org.acme.app;"
        and "status-contract:\n  image: maven:3.8-eclipse-temurin-17\n" in ci_text
        and "apt-get update -qq && apt-get install -y -qq python3 python3-yaml" in ci_text
        and "pip install" not in ci_text
        and first_other == "package org.acme.other;"
        and cp_bad.returncode == 1
        and "FAIL" in cp_bad.stdout
        and "org.nope" in cp_bad.stdout,
        detail,
    )


I9_CACHE_GITLAB_CI = (
    "stages: [test]\n"
    "variables:\n"
    '  PIP_CACHE_DIR: "$CI_PROJECT_DIR/.pip-cache"\n'
    "cache:\n"
    "  paths:\n"
    "    - $CI_PROJECT_DIR/.pip-cache/\n"
    "    - /var/cache/apt\n"
    ".setup:\n"
    "  before_script:\n"
    "    - echo setup\n"
    "unit:\n"
    "  stage: test\n"
    "  before_script: !reference [.setup, before_script]\n"
    "  script:\n"
    "    - pytest -q\n"
)
I9_CACHE_GITHUB_CI = (
    "name: ci\n"
    "on: [push]\n"
    "jobs:\n"
    "  unit:\n"
    "    runs-on: ubuntu-latest\n"
    "    steps:\n"
    "      - uses: actions/checkout@v4\n"
    "      - uses: actions/cache@v4\n"
    "        with:\n"
    "          path: |\n"
    "            node_modules\n"
    "            ~/.npm\n"
    "      - run: npm test\n"
)


def i9_ci_cache_scenario() -> bool:
    """A CI cache path inside the checkout (.pip-cache under $CI_PROJECT_DIR,
    node_modules under actions/cache) is an untracked directory the
    banned-word scan would read: --facts lists it as unignored, --write adds
    it to .gitignore, the next run reports OK. Paths outside the checkout
    (/var/cache/apt, ~/.npm) are dropped, and a !reference tag in the CI
    file does not break the parse."""
    ok = True
    cases = [
        ("gitlab", ".gitlab-ci.yml", I9_CACHE_GITLAB_CI, [".pip-cache/"]),
        ("github", ".github/workflows/ci.yml", I9_CACHE_GITHUB_CI, ["node_modules"]),
    ]
    for kind, ci_rel, ci_text, expected in cases:
        repo = SCRATCH_BASE / f"i9-cache-{kind}"
        (repo / ci_rel).parent.mkdir(parents=True)
        (repo / ci_rel).write_text(ci_text)
        (repo / ".gitignore").write_text("__pycache__/\n")
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        research = SCRATCH_BASE / f"i9-cache-{kind}-research"
        research.mkdir()
        run_json = SCRATCH_BASE / f"i9-cache-{kind}-run.json"
        run_json.write_text(
            json.dumps(
                {
                    "repo": str(repo),
                    "research": str(research),
                    "scratch": str(SCRATCH_BASE / f"i9-cache-{kind}-scratch"),
                    "python": sys.executable,
                    "skill": str(SKILL_ROOT),
                    "date": TODAY,
                }
            )
        )
        base_args = ["pipeline/stages/i9_install.py", "--run", str(run_json)]
        facts_before = json.loads(run([*base_args, "--facts"], SKILL_ROOT).stdout)
        cp_dry = run([*base_args, "--dry-run"], SKILL_ROOT)
        cp_write = run([*base_args, "--write"], SKILL_ROOT)
        facts_after = json.loads(run([*base_args, "--facts"], SKILL_ROOT).stdout)
        cp_again = run([*base_args, "--dry-run"], SKILL_ROOT)
        gitignore = (repo / ".gitignore").read_text()
        dry_line = next((l for l in cp_dry.stdout.splitlines() if ".gitignore" in l), "")
        again_line = next((l for l in cp_again.stdout.splitlines() if ".gitignore" in l), "")
        detail = (
            f"before={facts_before.get('ci_cache_unignored')} dry={dry_line!r} write_rc={cp_write.returncode} "
            f"after={facts_after.get('ci_cache_unignored')} again={again_line!r} gitignore={gitignore!r}"
        )
        ok &= record(
            f"78{'a' if kind == 'gitlab' else 'b'}: i9 ({kind}) lists an unignored CI cache path inside the "
            "checkout, --write adds it to .gitignore, the next run reports OK",
            facts_before.get("ci_cache_unignored") == expected
            and "UPDATE" in dry_line
            and f".gitignore (+ {', '.join(expected)}:" in dry_line
            and cp_write.returncode == 0
            and facts_after.get("ci_cache_unignored") == []
            and all(p in gitignore.splitlines() for p in expected)
            and again_line.strip().startswith("OK"),
            detail,
        )
    return ok


def i9_nested_python_scenario() -> bool:
    """A repo with no top-level tests/ but a pytest.ini at the root and a
    tests dir nested under a package (pkg/tests) must resolve tests_dir to
    the nested path, install the bridge test there (not at tests/), and
    must not be fooled by same-named dirs under .venv/.worktrees (skip-dirs)."""
    repo = SCRATCH_BASE / "i9-nested-py-repo"
    (repo / "pkg" / "tests").mkdir(parents=True)
    (repo / ".venv" / "tests").mkdir(parents=True)
    (repo / ".worktrees" / "x" / "tests").mkdir(parents=True)
    (repo / "pytest.ini").write_text("[pytest]\n")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)

    research = SCRATCH_BASE / "i9-nested-py-research"
    research.mkdir()
    run_json = SCRATCH_BASE / "i9-nested-py-run.json"
    run_json.write_text(
        json.dumps(
            {
                "repo": str(repo),
                "research": str(research),
                "scratch": str(SCRATCH_BASE / "i9-nested-py-scratch"),
                "python": sys.executable,
                "skill": str(SKILL_ROOT),
                "date": TODAY,
            }
        )
    )
    base_args = ["pipeline/stages/i9_install.py", "--run", str(run_json)]

    facts = json.loads(run([*base_args, "--facts"], SKILL_ROOT).stdout)
    cp_dry = run([*base_args, "--dry-run"], SKILL_ROOT)
    nested_line = f"  {'CREATE':14s} pkg/tests/test_status_contract.py"
    toplevel_line = f"  {'CREATE':14s} tests/test_status_contract.py"

    cp_write = run([*base_args, "--write"], SKILL_ROOT)
    nested_dst = repo / "pkg" / "tests" / "test_status_contract.py"

    detail = (
        f"facts.python_runner={facts.get('python_runner')} dry_has_nested={nested_line in cp_dry.stdout} "
        f"dry_has_toplevel={toplevel_line in cp_dry.stdout} write_rc={cp_write.returncode} "
        f"nested_dst_exists={nested_dst.is_file()} toplevel_tests_exists={(repo / 'tests').exists()}"
    )
    return record(
        "78c: i9 resolves a nested tests dir (pkg/tests) instead of tests/ when there is no "
        "top-level tests dir, is not fooled by same-named dirs under .venv/.worktrees, and "
        "installs the bridge test at the nested path",
        facts.get("python_runner") == {"detected": True, "via": "pytest.ini", "tests_dir": "pkg/tests"}
        and nested_line in cp_dry.stdout
        and toplevel_line not in cp_dry.stdout
        and cp_write.returncode == 0
        and nested_dst.is_file()
        and not (repo / "tests").exists(),
        detail,
    )


def banned_word_self_scan() -> bool:
    """Prove templates/ and selftest/ themselves are clean of FAIL-level
    identity tokens, using the same scanner the templates ship."""
    sys.path.insert(0, str(TEMPLATES))
    import contract_lib  # noqa: E402

    ok = True
    for base in (TEMPLATES, HERE):
        for path in base.rglob("*.py"):
            try:
                text = path.read_text()
            except UnicodeDecodeError:
                continue
            for lineno, line in enumerate(text.splitlines(), start=1):
                if "gt-allow:" in line:
                    continue
                for pat in contract_lib._FAIL_PATTERNS:
                    m = pat.search(line)
                    if m:
                        ok = record(
                            f"D16: {path.relative_to(SKILL_ROOT)}:{lineno} clean of banned words",
                            False,
                            f"{m.group(0)!r} in: {line.strip()}",
                        )
    if ok:
        record("D16: templates/ and selftest/ clean of FAIL-level banned words", True)
    return ok


def cited_line_scenarios() -> bool:
    """Prove check_cited_lines catches a note's file:line citation whose
    quote does not match the source line, and stays quiet when it does."""
    ok = True
    status_path = FIXTURE / "STATUS.yaml"

    cite_ok_claim = (
        "  - id: cite_ok\n"
        '    component: "citation test"\n'
        "    kind: live_state\n"
        '    command: "true"\n'
        "    note: 'see services/alpha/core.py:1 «GREETING = \"hello\"» for the literal.'\n"
    )
    cite_bad_claim = (
        "  - id: cite_bad\n"
        '    component: "citation test"\n'
        "    kind: live_state\n"
        '    command: "true"\n'
        "    note: 'see services/alpha/core.py:1 «GREETING = \"goodbye\"» for the literal.'\n"
    )

    with patched(status_path, "claims:\n", "claims:\n" + cite_ok_claim):
        cp = verify("sync", extra=["--show-warn"])
        ok &= record(
            "33: cited line contains its quote -> no cited-line issue, exit 0",
            cp.returncode == 0 and "does not contain" not in out(cp),
            out(cp),
        )

    with patched(status_path, "claims:\n", "claims:\n" + cite_bad_claim):
        cp = verify("sync", extra=["--show-warn"])
        ok &= record(
            "34: cited line does not contain its quote -> sync WARN with --show-warn, exit 0",
            cp.returncode == 0
            and "[WARN]" in out(cp)
            and "cite_bad" in out(cp)
            and "does not contain" in out(cp)
            and "remap_line_refs.py --apply" in out(cp),
            out(cp),
        )
        cp = verify("full")
        ok &= record(
            "35: same fault in full mode -> exit 1",
            cp.returncode == 1 and "[FAIL]" in out(cp) and "does not contain" in out(cp),
            out(cp),
        )

    cite_range_ok_claim = (
        "  - id: cite_range_ok\n"
        '    component: "citation test"\n'
        "    kind: live_state\n"
        '    command: "true"\n'
        "    note: 'see services/alpha/core.py:3-5 «return GREETING» for the literal.'\n"
    )
    cite_range_bad_claim = (
        "  - id: cite_range_bad\n"
        '    component: "citation test"\n'
        "    kind: live_state\n"
        '    command: "true"\n'
        "    note: 'see services/alpha/core.py:1-2 «return GREETING» for the literal.'\n"
    )

    with patched(status_path, "claims:\n", "claims:\n" + cite_range_ok_claim):
        cp = verify("full")
        ok &= record(
            "36: cited range A-B, quote on a line other than A -> exit 0",
            cp.returncode == 0 and "does not contain" not in out(cp),
            out(cp),
        )

    with patched(status_path, "claims:\n", "claims:\n" + cite_range_bad_claim):
        cp = verify("full")
        ok &= record(
            "36b: same quote outside the cited range -> exit 1",
            cp.returncode == 1
            and "cite_range_bad" in out(cp)
            and "core.py:1-2 does not contain" in out(cp),
            out(cp),
        )

    cite_semicolon_claim = (
        "  - id: cite_semicolon\n"
        '    component: "citation test"\n'
        "    kind: live_state\n"
        '    command: "true"\n'
        "    note: 'see services/alpha/core.py:1; `some command here` runs today'\n"
    )
    cite_semicolon_control_claim = (
        "  - id: cite_semicolon_control\n"
        '    component: "citation test"\n'
        "    kind: live_state\n"
        '    command: "true"\n'
        "    note: 'see services/alpha/core.py:1 `some command here`'\n"
    )

    with patched(status_path, "claims:\n", "claims:\n" + cite_semicolon_claim):
        cp = verify("full")
        ok &= record(
            "36c: quote window stops at the ; clause boundary -> no cited-line issue, exit 0",
            cp.returncode == 0 and "does not contain" not in out(cp),
            out(cp),
        )

    with patched(status_path, "claims:\n", "claims:\n" + cite_semicolon_control_claim):
        cp = verify("full")
        ok &= record(
            "36d: same note without the ; -> still checked, exit 1",
            cp.returncode == 1 and "does not contain" in out(cp),
            out(cp),
        )

    return ok


def main() -> int:
    build_fixture()
    ok = True
    ok &= happy_path()
    ok &= fault_scenarios()
    ok &= banned_words_exclude_scenarios()
    ok &= step4_scenarios()
    ok &= edge_case_scenarios()
    ok &= junit_scenarios()
    ok &= cited_line_scenarios()
    ok &= banned_word_self_scan()

    passed = sum(1 for _, o, _ in RESULTS if o)
    total = len(RESULTS)
    print(f"\n{passed}/{total} checks passed")

    if ok:
        shutil.rmtree(SCRATCH_BASE, ignore_errors=True)
        shutil.rmtree(FAKE_MVN_BIN, ignore_errors=True)
        print("fixture removed (all checks passed)")
    else:
        print(f"fixture kept for inspection: {FIXTURE}")

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
