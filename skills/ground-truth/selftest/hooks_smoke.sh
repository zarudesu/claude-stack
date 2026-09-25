#!/bin/bash
# Smoke test for ground-truth-sync-check.sh (Stop hook, which also runs
# the session guard), ground-truth-session-start.sh (SessionStart
# backstop) and ground-truth-edit-guard.sh (PreToolUse). Builds a
# throwaway git repo, feeds each hook realistic stdin JSON, and asserts
# exit codes / output against the documented contract. Prints PASS/FAIL
# per case; exits 0 only if every case passes.
set -u
# Legacy pre-edit gate cases explicitly opt in. The default is tested in process_checks.py.
export GT_STRICT_EDIT_GUARD=1

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATES_DIR="$(cd "$SCRIPT_DIR/../templates" && pwd)"
# GT_SELFTEST_TMP overrides where the throwaway repo is built.
SCRATCH_ROOT="${GT_SELFTEST_TMP:-${TMPDIR:-/tmp}}"
WORKDIR="$(mktemp -d "$SCRATCH_ROOT/gt-hooks-smoke.XXXXXX")"
REPO="$WORKDIR/repo"

PASS=0
FAIL=0

note() { echo "$1"; }
pass() { echo "PASS: $1"; PASS=$((PASS+1)); }
fail() { echo "FAIL: $1"; FAIL=$((FAIL+1)); }

# On success the whole scratch directory goes; on failure it stays and its
# path is printed at the end.
cleanup() { if [ "$FAIL" -eq 0 ]; then rm -rf "$WORKDIR"; fi; }
trap cleanup EXIT

setup_repo() {
  rm -rf "$REPO"
  mkdir -p "$REPO/.claude/hooks" "$REPO/tools/ground_truth"  # gt-allow: real install path, not prose
  cp "$TEMPLATES_DIR/ground-truth-sync-check.sh" "$REPO/.claude/hooks/ground-truth-sync-check.sh"  # gt-allow: real install path, not prose
  cp "$TEMPLATES_DIR/ground-truth-session-start.sh" "$REPO/.claude/hooks/ground-truth-session-start.sh"  # gt-allow: real install path, not prose
  chmod +x "$REPO/.claude/hooks/"*.sh  # gt-allow: real install path, not prose

  cat > "$REPO/tools/ground_truth/verify.py" <<'PYEOF'
#!/usr/bin/env python3
import os
import sys
sys.stdout.write("[FAIL] fake_claim: injected failure for smoke test\n")
if os.environ.get("GT_VERIFY_WARN") and "--show-warn" in sys.argv:
    sys.stdout.write("[WARN] stale_claim: path renamed; fix: update STATUS.yaml path field\n")
sys.exit(int(os.environ.get("GT_VERIFY_EXIT", "0")))
PYEOF
  chmod +x "$REPO/tools/ground_truth/verify.py"

  cat > "$REPO/STATUS.yaml" <<EOF
schema_version: 1
meta:
  repo: gt-hooks-smoke
  enforcement: ${ENFORCEMENT:-advisory}
  last_audited: "2026-09-04"
  coverage:
    roots: []
    runner: none
claims: []
EOF

  (cd "$REPO" && git init -q && git add -A && git -c user.email=t@example.com -c user.name=t commit -q -m init)
}

dirty_repo() {
  echo "# dirty" >> "$REPO/README.md"
}

run_stop_hook() {
  local stop_active="$1"
  local input
  input=$(printf '{"session_id":"x","cwd":"%s","hook_event_name":"Stop","stop_hook_active":%s}' "$REPO" "$stop_active")
  printf '%s' "$input" | GT_VERIFY_EXIT="${GT_VERIFY_EXIT:-0}" bash "$REPO/.claude/hooks/ground-truth-sync-check.sh"  # gt-allow: real install path, not prose
}

run_session_start_hook() {
  local input
  input=$(printf '{"session_id":"x","cwd":"%s","hook_event_name":"SessionStart"}' "$REPO")
  printf '%s' "$input" | GT_VERIFY_EXIT="${GT_VERIFY_EXIT:-0}" bash "$REPO/.claude/hooks/ground-truth-session-start.sh"  # gt-allow: real install path, not prose
}

# Case 1: clean tree -> exit 0 quietly (no drift text on stderr)
export ENFORCEMENT=advisory GT_VERIFY_EXIT=0
setup_repo
out=$(run_stop_hook false 2>&1); rc=$?
if [ $rc -eq 0 ] && ! printf '%s' "$out" | grep -q "STATUS.yaml"; then
  pass "clean tree -> exit 0 quietly"
else
  fail "clean tree -> exit 0 quietly (rc=$rc out=$out)"
fi

# Case 2: dirty tree + verifier fail + advisory -> exit 0 with stderr text
export ENFORCEMENT=advisory GT_VERIFY_EXIT=1
setup_repo
dirty_repo
out=$(run_stop_hook false 2>&1); rc=$?
if [ $rc -eq 0 ] && printf '%s' "$out" | grep -q "advisory"; then
  pass "dirty + fail + advisory -> exit 0 with stderr text"
else
  fail "dirty + fail + advisory -> exit 0 with stderr text (rc=$rc out=$out)"
fi

# Case 3: dirty + fail + blocking -> exit 2
export ENFORCEMENT=blocking GT_VERIFY_EXIT=1
setup_repo
dirty_repo
out=$(run_stop_hook false 2>&1); rc=$?
if [ $rc -eq 2 ] && printf '%s' "$out" | grep -q "STATUS.yaml out of sync"; then
  pass "dirty + fail + blocking -> exit 2"
else
  fail "dirty + fail + blocking -> exit 2 (rc=$rc out=$out)"
fi

# Case 4: stop_hook_active true -> exit 0 immediately, even dirty+fail+blocking
export ENFORCEMENT=blocking GT_VERIFY_EXIT=1
setup_repo
dirty_repo
out=$(run_stop_hook true 2>&1); rc=$?
if [ $rc -eq 0 ] && [ -z "$out" ]; then
  pass "stop_hook_active true -> exit 0 immediately"
else
  fail "stop_hook_active true -> exit 0 immediately (rc=$rc out=$out)"
fi

# Case 5: SessionStart backstop with failing verifier -> exit 0, stdout has markdown block
export ENFORCEMENT=advisory GT_VERIFY_EXIT=1
setup_repo
out=$(run_session_start_hook); rc=$?
if [ $rc -eq 0 ] && printf '%s' "$out" | grep -q '```' && printf '%s' "$out" | grep -q "currently out of sync"; then
  pass "SessionStart failing verifier -> exit 0 with markdown block"
else
  fail "SessionStart failing verifier -> exit 0 with markdown block (rc=$rc out=$out)"
fi

# ---------------------------------------------------------------------
# Guard cases. The repo grows a real module, a real test and a real claim
# covering it; the fake verify.py stays (the sync step is not what these
# cases are about), while the session guard and the edit guard are the
# templates themselves.
# ---------------------------------------------------------------------

write_pricing() {
  cat > "$REPO/app/pricing.py" <<EOF
def monthly_price(plan):
    if plan == "basic":
        return ${1:-100}
    return 200
EOF
}

setup_guard_repo() {
  setup_repo
  cp "$TEMPLATES_DIR/contract_lib.py" "$REPO/tools/ground_truth/contract_lib.py"
  cp "$TEMPLATES_DIR/gt_session_guard.py" "$REPO/tools/ground_truth/gt_session_guard.py"
  cp "$TEMPLATES_DIR/gt_edit_guard.py" "$REPO/tools/ground_truth/gt_edit_guard.py"
  cp "$TEMPLATES_DIR/ground-truth-edit-guard.sh" "$REPO/.claude/hooks/ground-truth-edit-guard.sh"  # gt-allow: real install path, not prose
  chmod +x "$REPO/.claude/hooks/"*.sh  # gt-allow: real install path, not prose

  mkdir -p "$REPO/app" "$REPO/tests"
  write_pricing 100
  cat > "$REPO/tests/test_pricing.py" <<'PYEOF'
from app.pricing import monthly_price


def test_basic_price():
    assert monthly_price("basic") == 100
PYEOF
  echo "# gt-hooks-smoke" > "$REPO/README.md"

  cat > "$REPO/STATUS.yaml" <<EOF
schema_version: 1
meta:
  repo: gt-hooks-smoke
  enforcement: ${ENFORCEMENT:-advisory}
  last_audited: "2026-09-06"
  coverage:
    roots:
      - "app/"
      - "tests/"
    runner: pytest
claims:
  - id: monthly_price_basic
    component: "monthly price of the basic plan"
    kind: status
    status: implemented
    path: "app/pricing.py"
    check_kind: pytest
    check: "tests/test_pricing.py::test_basic_price"
EOF
  (cd "$REPO" && git add -A && git -c user.email=t@example.com -c user.name=t commit -q -m contract)
}

append_claim() {
  cat >> "$REPO/STATUS.yaml" <<EOF
  - id: $1
    component: "yearly price of the basic plan"
    kind: status
    status: implemented
    path: "app/pricing.py"
    check_kind: pytest
    check: "tests/test_pricing.py::test_basic_price"
EOF
  if [ "${2:-}" = "with-mutation" ]; then
    cat >> "$REPO/STATUS.yaml" <<EOF
    mutation:
      file: "app/pricing.py"
      find: "return 100"
      replace: "return 999"
EOF
  fi
}

run_edit_hook() {
  local input
  input=$(printf '{"session_id":"x","cwd":"%s","hook_event_name":"PreToolUse","tool_name":"Edit","tool_input":{"file_path":"%s"}}' "$REPO" "$1")
  (cd "$REPO" && printf '%s' "$input" | bash "$REPO/.claude/hooks/ground-truth-edit-guard.sh")  # gt-allow: real install path, not prose
}

# Case 6: edit guard, uncovered file under the roots, blocking -> exit 2
export ENFORCEMENT=blocking GT_VERIFY_EXIT=0
setup_guard_repo
out=$(run_edit_hook "$REPO/app/billing.py" 2>&1); rc=$?
if [ $rc -eq 2 ] && printf '%s' "$out" | grep -q "no claim covers it"; then
  pass "edit guard: uncovered file + blocking -> exit 2"
else
  fail "edit guard: uncovered file + blocking -> exit 2 (rc=$rc out=$out)"
fi

# Case 7: same file, advisory -> exit 0 but the hint is printed
export ENFORCEMENT=advisory GT_VERIFY_EXIT=0
setup_guard_repo
out=$(run_edit_hook "$REPO/app/billing.py" 2>&1); rc=$?
if [ $rc -eq 0 ] && printf '%s' "$out" | grep -q "(advisory)"; then
  pass "edit guard: uncovered file + advisory -> exit 0 with the hint"
else
  fail "edit guard: uncovered file + advisory -> exit 0 with the hint (rc=$rc out=$out)"
fi

# Case 8: STATUS.yaml already dirty -> the claim is being written, exit 0
export ENFORCEMENT=blocking GT_VERIFY_EXIT=0
setup_guard_repo
echo "# claim in progress" >> "$REPO/STATUS.yaml"
out=$(run_edit_hook "$REPO/app/billing.py" 2>&1); rc=$?
if [ $rc -eq 0 ] && [ -z "$out" ]; then
  pass "edit guard: STATUS.yaml dirty -> exit 0"
else
  fail "edit guard: STATUS.yaml dirty -> exit 0 (rc=$rc out=$out)"
fi

# Case 9: file a claim covers -> exit 0
export ENFORCEMENT=blocking GT_VERIFY_EXIT=0
setup_guard_repo
out=$(run_edit_hook "$REPO/app/pricing.py" 2>&1); rc=$?
if [ $rc -eq 0 ] && [ -z "$out" ]; then
  pass "edit guard: covered file -> exit 0"
else
  fail "edit guard: covered file -> exit 0 (rc=$rc out=$out)"
fi

# Case 10: a test file is what claims point at, never a claim subject
export ENFORCEMENT=blocking GT_VERIFY_EXIT=0
setup_guard_repo
out=$(run_edit_hook "$REPO/tests/test_pricing.py" 2>&1); rc=$?
if [ $rc -eq 0 ] && [ -z "$out" ]; then
  pass "edit guard: test file -> exit 0"
else
  fail "edit guard: test file -> exit 0 (rc=$rc out=$out)"
fi

# Case 11: file outside the coverage roots -> exit 0
export ENFORCEMENT=blocking GT_VERIFY_EXIT=0
setup_guard_repo
out=$(run_edit_hook "$REPO/README.md" 2>&1); rc=$?
if [ $rc -eq 0 ] && [ -z "$out" ]; then
  pass "edit guard: file outside coverage roots -> exit 0"
else
  fail "edit guard: file outside coverage roots -> exit 0 (rc=$rc out=$out)"
fi

# Case 12: session guard, touched claim whose check is still green -> exit 0
export ENFORCEMENT=blocking GT_VERIFY_EXIT=0
setup_guard_repo
echo "# tuned for the new plan" >> "$REPO/app/pricing.py"
out=$(run_stop_hook false 2>&1); rc=$?
if [ $rc -eq 0 ]; then
  pass "session guard: touched claim, check green -> exit 0"
else
  fail "session guard: touched claim, check green -> exit 0 (rc=$rc out=$out)"
fi

# Case 13: the same claim's check turned red by this change -> exit 2
export ENFORCEMENT=blocking GT_VERIFY_EXIT=0
setup_guard_repo
write_pricing 111
out=$(run_stop_hook false 2>&1); rc=$?
if [ $rc -eq 2 ] && printf '%s' "$out" | grep -q "check FAILED" \
  && printf '%s' "$out" | grep -q "fix: make the check green"; then
  pass "session guard: touched claim, check red -> exit 2"
else
  fail "session guard: touched claim, check red -> exit 2 (rc=$rc out=$out)"
fi

# Case 19: the check-red recipe names the exact command, its cwd, the
# claim's target files, and a tail of the check's own output -- so the
# same FAIL does not repeat for another turn without a next step.
export ENFORCEMENT=blocking GT_VERIFY_EXIT=0
setup_guard_repo
write_pricing 111
out=$(run_stop_hook false 2>&1); rc=$?
if [ $rc -eq 2 ] \
  && printf '%s' "$out" | grep -q "command:.*pytest tests/test_pricing.py::test_basic_price" \
  && printf '%s' "$out" | grep -q "(cwd=" \
  && printf '%s' "$out" | grep -q "targets: app/pricing.py, tests/test_pricing.py" \
  && printf '%s' "$out" | grep -q "test_basic_price"; then
  pass "session guard: check red -> recipe names command, cwd and target files"
else
  fail "session guard: check red -> recipe names command, cwd and target files (rc=$rc out=$out)"
fi

# Case 14: new implemented claim with no mutation -> exit 2
export ENFORCEMENT=blocking GT_VERIFY_EXIT=0
setup_guard_repo
append_claim yearly_price_basic
out=$(run_stop_hook false 2>&1); rc=$?
if [ $rc -eq 2 ] && printf '%s' "$out" | grep -q "needs mutation" \
  && printf '%s' "$out" | grep -q "fix: add mutation: {file, find, replace} to claim yearly_price_basic"; then
  pass "session guard: new implemented claim without mutation -> exit 2"
else
  fail "session guard: new implemented claim without mutation -> exit 2 (rc=$rc out=$out)"
fi

# Case 15: valid mutation declaration -> local hook leaves proof to isolated CI.
export ENFORCEMENT=blocking GT_VERIFY_EXIT=0
setup_guard_repo
append_claim yearly_price_basic with-mutation
out=$(run_stop_hook false 2>&1); rc=$?
if [ $rc -eq 0 ] && [ -z "$(cd "$REPO" && git status --porcelain -- app/pricing.py)" ] \
  && printf '%s' "$out" | grep -q "mutation proof pending"; then
  pass "session guard: new claim -> exit 0, explicit pending proof, source untouched"
else
  fail "session guard: new claim with a red-proving mutation -> exit 0, file restored (rc=$rc out=$out)"
fi

# Case 16: textual test-name coverage is advisory, not behavioral proof.
export ENFORCEMENT=blocking GT_VERIFY_EXIT=0
setup_guard_repo
cat >> "$REPO/app/pricing.py" <<'PYEOF'


def yearly_price(plan):
    return monthly_price(plan) * 12
PYEOF
out=$(python3 "$REPO/tools/ground_truth/gt_session_guard.py" --root "$REPO" --mode hook 2>&1); rc=$?
if [ $rc -eq 0 ] && printf '%s' "$out" | grep -q "no test reference"; then
  pass "session guard: new definition without a test reference -> advisory, exit 0"
else
  fail "session guard: new definition without a test reference -> advisory, exit 0 (rc=$rc out=$out)"
fi

# Case 17: the guard's own failure never blocks a session
export ENFORCEMENT=blocking GT_VERIFY_EXIT=0
setup_guard_repo
printf 'meta: [unclosed\n' > "$REPO/STATUS.yaml"
out=$(run_stop_hook false 2>&1); rc=$?
if [ $rc -eq 0 ] && printf '%s' "$out" | grep -q "internal error"; then
  pass "session guard: unparsable STATUS.yaml -> exit 0 with an internal-error warning"
else
  fail "session guard: unparsable STATUS.yaml -> exit 0 with an internal-error warning (rc=$rc out=$out)"
fi

# Case 18: SIGTERM while the guard holds a mutation on disk -> the file is
# restored before the guard dies. The Stop hook's own timeout arrives as a
# signal, and a finally clause does not run for one.
export ENFORCEMENT=blocking GT_VERIFY_EXIT=0
setup_guard_repo
cat > "$REPO/tests/test_pricing.py" <<'SLOWEOF'
import time

from app.pricing import monthly_price


def test_basic_price():
    if monthly_price("basic") != 100:
        time.sleep(15)
    assert monthly_price("basic") == 100
SLOWEOF
(cd "$REPO" && git add -A && git -c user.email=t@example.com -c user.name=t commit -q -m slow-check)
append_claim yearly_price_basic with-mutation
cp "$REPO/app/pricing.py" "$WORKDIR/pricing.before"
python3 "$REPO/tools/ground_truth/gt_session_guard.py" --root "$REPO" --mode ci --base HEAD >/dev/null 2>&1 &
guard_pid=$!
sleep 3
kill -TERM "$guard_pid" 2>/dev/null
wait "$guard_pid"; rc=$?
if [ $rc -ne 0 ] && cmp -s "$WORKDIR/pricing.before" "$REPO/app/pricing.py"; then
  pass "session guard: SIGTERM mid-mutation -> file restored, exit non-zero"
else
  fail "session guard: SIGTERM mid-mutation -> file restored, exit non-zero (rc=$rc)"
fi

# ---------------------------------------------------------------------
# Hard-gate cases (gt_hook_state.py): the blast gate and the probe
# budget are hard regardless of meta.enforcement, and the Stop hook's
# red-counter escalation and the SessionStart ack-reset both key off the
# same state file. setup_guard_repo alone never installs gt_hook_state.py
# (case 9's "covered file -> exit 0" depends on it being absent, so the
# blast gate fails open via ImportError); add_gt_hook_state layers it in
# only for the cases below.
# ---------------------------------------------------------------------

add_gt_hook_state() {
  cp "$TEMPLATES_DIR/gt_hook_state.py" "$REPO/tools/ground_truth/gt_hook_state.py"  # gt-allow: real install path, not prose
}

add_blast_radius() {
  cp "$TEMPLATES_DIR/blast_radius.py" "$REPO/tools/ground_truth/blast_radius.py"  # gt-allow: real install path, not prose
}

setup_hard_guard_repo() {
  setup_guard_repo
  add_gt_hook_state
  add_blast_radius
}

run_edit_hook_as() {
  local sid="$1" path="$2"
  local input
  input=$(printf '{"session_id":"%s","cwd":"%s","hook_event_name":"PreToolUse","tool_name":"Edit","tool_input":{"file_path":"%s"}}' "$sid" "$REPO" "$path")
  (cd "$REPO" && printf '%s' "$input" | bash "$REPO/.claude/hooks/ground-truth-edit-guard.sh")  # gt-allow: real install path, not prose
}

add_probe() {
  mkdir -p "$REPO/tools/ground_truth/probes"  # gt-allow: real install path, not prose
  cat > "$REPO/tools/ground_truth/probes/$1" <<PYEOF
import sys
sys.exit($2)
PYEOF
}

add_remap() {
  cp "$TEMPLATES_DIR/remap_line_refs.py" "$REPO/tools/ground_truth/remap_line_refs.py"  # gt-allow: real install path, not prose
}

setup_remap_repo() {
  setup_repo
  add_remap
  mkdir -p "$REPO/app"
  write_pricing 100
  cat > "$REPO/STATUS.yaml" <<EOF
schema_version: 1
meta:
  repo: gt-hooks-smoke
  enforcement: ${ENFORCEMENT:-advisory}
  last_audited: "2026-09-09"
  coverage:
    roots: []
    runner: none
claims:
  - id: monthly_price_basic
    component: "monthly price of the basic plan"
    kind: status
    status: implemented
    path: "app/pricing.py"
    note: "see app/pricing.py:3 for the basic-plan return"
EOF
  (cd "$REPO" && git add -A && git -c user.email=t@example.com -c user.name=t commit -q -m contract)
}

run_remap_check() {
  (cd "$REPO" && python3 tools/ground_truth/remap_line_refs.py --check --base HEAD STATUS.yaml)  # gt-allow: real install path, not prose
}

shift_pricing_lines() {
  { printf '# shim\n# shim2\n'; cat "$REPO/app/pricing.py"; } > "$WORKDIR/pricing.shifted" && mv "$WORKDIR/pricing.shifted" "$REPO/app/pricing.py"
}

# Case E1: covered file, no ack yet this session -> exit 2, names the
# claim and the exact blast_radius.py command to run.
export ENFORCEMENT=blocking GT_VERIFY_EXIT=0
setup_hard_guard_repo
out=$(run_edit_hook "$REPO/app/pricing.py" 2>&1); rc=$?
if [ $rc -eq 2 ] && printf '%s' "$out" | grep -q "monthly_price_basic" && printf '%s' "$out" | grep -q "blast_radius.py app/pricing.py"; then
  pass "E1: blast gate, no ack yet -> exit 2 naming the claim and blast_radius.py"
else
  fail "E1: blast gate, no ack yet -> exit 2 naming the claim and blast_radius.py (rc=$rc out=$out)"
fi

# Case E2: once blast_radius.py <rel> has run and reported on the file
# this session, the edit goes through -- the ack comes from the real
# tool, not from the state CLI's own blast-ack verb.
export ENFORCEMENT=blocking GT_VERIFY_EXIT=0
setup_hard_guard_repo
(cd "$REPO" && python3 tools/ground_truth/blast_radius.py --root "$REPO" app/pricing.py >/dev/null)  # gt-allow: real install path, not prose
out=$(run_edit_hook "$REPO/app/pricing.py" 2>&1); rc=$?
if [ $rc -eq 0 ] && [ -z "$out" ]; then
  pass "E2: blast_radius.py app/pricing.py acks the file this session -> exit 0"
else
  fail "E2: blast_radius.py app/pricing.py acks the file this session -> exit 0 (rc=$rc out=$out)"
fi

# Case E3: SessionStart resets acks -- a restart (crash, compaction) must
# not trust a blast_radius read from a turn the session no longer remembers.
export ENFORCEMENT=blocking GT_VERIFY_EXIT=0
setup_hard_guard_repo
python3 "$REPO/tools/ground_truth/gt_hook_state.py" --root "$REPO" blast-ack app/pricing.py  # gt-allow: real install path, not prose
sleep 1
run_session_start_hook >/dev/null
out=$(run_edit_hook "$REPO/app/pricing.py" 2>&1); rc=$?
if [ $rc -eq 2 ] && printf '%s' "$out" | grep -q "blast_radius.py app/pricing.py"; then
  pass "E3: SessionStart resets acks -> a prior ack no longer covers the file"
else
  fail "E3: SessionStart resets acks -> a prior ack no longer covers the file (rc=$rc out=$out)"
fi

# Case E4: acking by claim id covers a multi-path claim -- both paths
# become editable, not just the one path.py happens to name.
export ENFORCEMENT=blocking GT_VERIFY_EXIT=0
setup_hard_guard_repo
cat >> "$REPO/STATUS.yaml" <<EOF
  - id: shared_pricing_billing
    component: "pricing and billing share one claim"
    kind: status
    status: implemented
    path:
      - "app/pricing.py"
      - "app/billing.py"
    check_kind: pytest
    check: "tests/test_pricing.py::test_basic_price"
EOF
(cd "$REPO" && python3 tools/ground_truth/blast_radius.py --root "$REPO" shared_pricing_billing >/dev/null)  # gt-allow: real install path, not prose
out1=$(run_edit_hook "$REPO/app/pricing.py" 2>&1); rc1=$?
out2=$(run_edit_hook "$REPO/app/billing.py" 2>&1); rc2=$?
if [ $rc1 -eq 0 ] && [ $rc2 -eq 0 ] && [ -z "$out1" ] && [ -z "$out2" ]; then
  pass "E4: ack by claim id covers a multi-path claim, both paths editable"
else
  fail "E4: ack by claim id covers a multi-path claim, both paths editable (rc1=$rc1 rc2=$rc2 out1=$out1 out2=$out2)"
fi

# Case E5: an unknown session id -- one gt_hook_state has no session-start
# record for -- still lets a fresh ack through via the 4h fallback.
export ENFORCEMENT=blocking GT_VERIFY_EXIT=0
setup_hard_guard_repo
(cd "$REPO" && python3 tools/ground_truth/blast_radius.py --root "$REPO" app/pricing.py >/dev/null)  # gt-allow: real install path, not prose
out=$(run_edit_hook_as never-started "$REPO/app/pricing.py" 2>&1); rc=$?
if [ $rc -eq 0 ] && [ -z "$out" ]; then
  pass "E5: unknown session id with a fresh ack -> exit 0 (4h fallback)"
else
  fail "E5: unknown session id with a fresh ack -> exit 0 (4h fallback) (rc=$rc out=$out)"
fi

# Case E6: a claim whose path: is a list gates every path in the list,
# each independently -- acking one path is not asked for here, only that
# the second, unacked path is gated under the same claim id.
export ENFORCEMENT=blocking GT_VERIFY_EXIT=0
setup_hard_guard_repo
cat >> "$REPO/STATUS.yaml" <<EOF
  - id: shared_pricing_billing
    component: "pricing and billing share one claim"
    kind: status
    status: implemented
    path:
      - "app/pricing.py"
      - "app/billing.py"
    check_kind: pytest
    check: "tests/test_pricing.py::test_basic_price"
EOF
out=$(run_edit_hook "$REPO/app/billing.py" 2>&1); rc=$?
if [ $rc -eq 2 ] && printf '%s' "$out" | grep -q "shared_pricing_billing" && printf '%s' "$out" | grep -q "blast_radius.py app/billing.py"; then
  pass "E6: a multi-path claim's path: list gates its second path too"
else
  fail "E6: a multi-path claim's path: list gates its second path too (rc=$rc out=$out)"
fi

# Case E7: the blast gate stays hard under advisory enforcement -- only
# the uncovered-file check (cases 6/7) bends to meta.enforcement.
export ENFORCEMENT=advisory GT_VERIFY_EXIT=0
setup_hard_guard_repo
out=$(run_edit_hook "$REPO/app/pricing.py" 2>&1); rc=$?
if [ $rc -eq 2 ] && printf '%s' "$out" | grep -q "blast_radius.py app/pricing.py"; then
  pass "E7: blast gate stays hard under advisory enforcement"
else
  fail "E7: blast gate stays hard under advisory enforcement (rc=$rc out=$out)"
fi

# Case E8: a claim whose path: is a directory gates every file under it;
# acking that claim by claim id (the form the SessionStart card
# advertises) opens the gate for a file inside the directory too, not
# only for the directory path itself.
export ENFORCEMENT=blocking GT_VERIFY_EXIT=0
setup_hard_guard_repo
mkdir -p "$REPO/app/dir"
cat > "$REPO/app/dir/mod.py" <<'PYEOF'
def f():
    return 1
PYEOF
cat >> "$REPO/STATUS.yaml" <<EOF
  - id: claim_dir
    component: "a directory-scoped claim"
    kind: status
    status: implemented
    path: "app/dir"
    check_kind: pytest
    check: "tests/test_pricing.py::test_basic_price"
EOF
(cd "$REPO" && python3 tools/ground_truth/blast_radius.py --root "$REPO" claim_dir >/dev/null)  # gt-allow: real install path, not prose
out=$(run_edit_hook "$REPO/app/dir/mod.py" 2>&1); rc=$?
if [ $rc -eq 0 ] && [ -z "$out" ]; then
  pass "E8: directory-scoped claim, ack by claim id, edit a file inside -> exit 0"
else
  fail "E8: directory-scoped claim, ack by claim id, edit a file inside -> exit 0 (rc=$rc out=$out)"
fi

# Case E9: a claim whose path sits outside meta.coverage.roots -- the
# blast gate fires regardless, because the roots filter belongs to the
# uncovered-file rule only, never to the covered-file rule.
export ENFORCEMENT=blocking GT_VERIFY_EXIT=0
setup_hard_guard_repo
echo "FROM scratch" > "$REPO/Dockerfile"
cat >> "$REPO/STATUS.yaml" <<EOF
  - id: docker_base_image
    component: "container base image"
    kind: status
    status: implemented
    path: "Dockerfile"
    check_kind: pytest
    check: "tests/test_pricing.py::test_basic_price"
EOF
out=$(run_edit_hook "$REPO/Dockerfile" 2>&1); rc=$?
if [ $rc -eq 2 ] && printf '%s' "$out" | grep -q "docker_base_image" && printf '%s' "$out" | grep -q "blast_radius.py Dockerfile"; then
  pass "E9: claim outside coverage roots -- blast gate fires until acked"
else
  fail "E9: claim outside coverage roots -- blast gate fires until acked (rc=$rc out=$out)"
fi
(cd "$REPO" && python3 tools/ground_truth/blast_radius.py --root "$REPO" Dockerfile >/dev/null)  # gt-allow: real install path, not prose
out2=$(run_edit_hook "$REPO/Dockerfile" 2>&1); rc2=$?
if [ $rc2 -eq 0 ] && [ -z "$out2" ]; then
  pass "E9: blast_radius.py Dockerfile acks it -> exit 0"
else
  fail "E9: blast_radius.py Dockerfile acks it -> exit 0 (rc2=$rc2 out2=$out2)"
fi

# Case E10: the git-dir is unwritable and no state file exists yet --
# neither the edit guard nor blast_radius.py has anywhere to record an
# ack, so both fail open instead of denying the edit with no way to
# comply. Permissions are restored right after both runs, before the
# pass/fail check, so a failing case never leaves a read-only fixture.
export ENFORCEMENT=blocking GT_VERIFY_EXIT=0
setup_hard_guard_repo
chmod a-w "$REPO/.git"
out=$(run_edit_hook "$REPO/app/pricing.py" 2>&1); rc=$?
out2=$(cd "$REPO" && python3 tools/ground_truth/blast_radius.py --root "$REPO" app/pricing.py 2>&1); rc2=$?  # gt-allow: real install path, not prose
chmod u+w "$REPO/.git"
if [ $rc -eq 0 ] && [ -z "$out" ] && [ $rc2 -eq 0 ] && printf '%s' "$out2" | grep -q "state not recorded"; then
  pass "E10: unwritable git-dir, no state file -- edit and blast_radius.py both fail open"
else
  fail "E10: unwritable git-dir, no state file -- edit and blast_radius.py both fail open (rc=$rc rc2=$rc2 out=$out out2=$out2)"
fi

# Case E11: as E10, but SessionStart has already created the state file
# before the git-dir turns read-only -- the normal state of an installed
# repo. A mode check on the file alone would call it writable while the
# atomic replace needs the directory, so the probe must attempt the real
# write; both paths still fail open.
export ENFORCEMENT=blocking GT_VERIFY_EXIT=0
setup_hard_guard_repo
python3 "$REPO/tools/ground_truth/gt_hook_state.py" --root "$REPO" session-start smoke-e11  # gt-allow: real install path, not prose
chmod a-w "$REPO/.git"
out=$(run_edit_hook "$REPO/app/pricing.py" 2>&1); rc=$?
out2=$(cd "$REPO" && python3 tools/ground_truth/blast_radius.py --root "$REPO" app/pricing.py 2>&1); rc2=$?  # gt-allow: real install path, not prose
chmod u+w "$REPO/.git"
if [ $rc -eq 0 ] && [ -z "$out" ] && [ $rc2 -eq 0 ] && printf '%s' "$out2" | grep -q "state not recorded"; then
  pass "E11: unwritable git-dir, state file already present -- edit and blast_radius.py both fail open"
else
  fail "E11: unwritable git-dir, state file already present -- edit and blast_radius.py both fail open (rc=$rc rc2=$rc2 out=$out out2=$out2)"
fi

# Case P1: a probe path that does not exist yet -- a new probe -> exit 0,
# nothing recorded against the session's rewrite budget.
export ENFORCEMENT=blocking GT_VERIFY_EXIT=0
setup_hard_guard_repo
out=$(run_edit_hook "$REPO/tools/ground_truth/probes/probe_new.py" 2>&1); rc=$?
if [ $rc -eq 0 ] && [ -z "$out" ]; then
  pass "P1: new probe path (file absent) -> exit 0"
else
  fail "P1: new probe path (file absent) -> exit 0 (rc=$rc out=$out)"
fi

# Case P2: a red probe is never budgeted, no matter how many times it is
# rewritten -- only a probe that is green when run counts at all.
export ENFORCEMENT=blocking GT_VERIFY_EXIT=0
setup_hard_guard_repo
add_probe probe_red.py 1
out=$(run_edit_hook "$REPO/tools/ground_truth/probes/probe_red.py" 2>&1); rc=$?
out2=$(run_edit_hook "$REPO/tools/ground_truth/probes/probe_red.py" 2>&1); rc2=$?
if [ $rc -eq 0 ] && [ $rc2 -eq 0 ] && [ -z "$out" ] && [ -z "$out2" ]; then
  pass "P2: a red probe is always editable, never budgeted"
else
  fail "P2: a red probe is always editable, never budgeted (rc=$rc rc2=$rc2 out=$out out2=$out2)"
fi

# Case P3: rewriting a probe already in this session's rewritten set is
# free -- it does not consume a second slot of the budget.
export ENFORCEMENT=blocking GT_VERIFY_EXIT=0
setup_hard_guard_repo
add_probe probe_a.py 0
run_edit_hook "$REPO/tools/ground_truth/probes/probe_a.py" >/dev/null 2>&1
out=$(run_edit_hook "$REPO/tools/ground_truth/probes/probe_a.py" 2>&1); rc=$?
if [ $rc -eq 0 ] && [ -z "$out" ]; then
  pass "P3: rewriting the same green probe again this session is free"
else
  fail "P3: rewriting the same green probe again this session is free (rc=$rc out=$out)"
fi

# Case P4: GT_PROBE_BUDGET=0 -> even the first distinct green probe this
# session exceeds the owner-set budget -> exit 2.
export ENFORCEMENT=blocking GT_VERIFY_EXIT=0 GT_PROBE_BUDGET=0
setup_hard_guard_repo
add_probe probe_a.py 0
out=$(run_edit_hook "$REPO/tools/ground_truth/probes/probe_a.py" 2>&1); rc=$?
unset GT_PROBE_BUDGET
if [ $rc -eq 2 ] && printf '%s' "$out" | grep -q "0 of 0 allowed" && printf '%s' "$out" | grep -q "ESCALATE"; then
  pass "P4: GT_PROBE_BUDGET=0 -> first green probe -> exit 2"
else
  fail "P4: GT_PROBE_BUDGET=0 -> first green probe -> exit 2 (rc=$rc out=$out)"
fi

# Case P5: a green probe's first rewrite, within the default budget ->
# exit 0.
export ENFORCEMENT=blocking GT_VERIFY_EXIT=0
setup_hard_guard_repo
add_probe probe_a.py 0
out=$(run_edit_hook "$REPO/tools/ground_truth/probes/probe_a.py" 2>&1); rc=$?
if [ $rc -eq 0 ] && [ -z "$out" ]; then
  pass "P5: first green-probe rewrite within budget -> exit 0"
else
  fail "P5: first green-probe rewrite within budget -> exit 0 (rc=$rc out=$out)"
fi

# Case P6: a third distinct green probe exceeds the default budget of 2
# -> exit 2, naming the already-used probes and an ESCALATE report line.
export ENFORCEMENT=blocking GT_VERIFY_EXIT=0
setup_hard_guard_repo
add_probe probe_a.py 0
add_probe probe_b.py 0
add_probe probe_c.py 0
run_edit_hook "$REPO/tools/ground_truth/probes/probe_a.py" >/dev/null 2>&1
run_edit_hook "$REPO/tools/ground_truth/probes/probe_b.py" >/dev/null 2>&1
out=$(run_edit_hook "$REPO/tools/ground_truth/probes/probe_c.py" 2>&1); rc=$?
if [ $rc -eq 2 ] && printf '%s' "$out" | grep -q "2 of 2 allowed" && printf '%s' "$out" | grep -q "ESCALATE"; then
  pass "P6: a third distinct green-probe rewrite exceeds the budget -> exit 2"
else
  fail "P6: a third distinct green-probe rewrite exceeds the budget -> exit 2 (rc=$rc out=$out)"
fi

# ---------------------------------------------------------------------
# Bash-write cases (defect j): the matcher now includes Bash, and the
# guard applies the same gate to a path a write-shaped command touches.
# ---------------------------------------------------------------------

run_bash_hook() {
  local cmd="$1"
  local input
  input=$(printf '{"session_id":"x","cwd":"%s","hook_event_name":"PreToolUse","tool_name":"Bash","tool_input":{"command":"%s"}}' "$REPO" "$cmd")
  (cd "$REPO" && printf '%s' "$input" | bash "$REPO/.claude/hooks/ground-truth-edit-guard.sh")  # gt-allow: real install path, not prose
}

# Case J1: Bash heredoc write to an uncovered file under the roots,
# blocking -> exit 2, same message shape as the Edit case (Case 6).
export ENFORCEMENT=blocking GT_VERIFY_EXIT=0
setup_guard_repo
out=$(run_bash_hook "cat > app/billing.py <<EOF" 2>&1); rc=$?
if [ $rc -eq 2 ] && printf '%s' "$out" | grep -q "no claim covers it" && printf '%s' "$out" | grep -q "Bash"; then
  pass "J1: Bash heredoc write to uncovered file + blocking -> exit 2"
else
  fail "J1: Bash heredoc write to uncovered file + blocking -> exit 2 (rc=$rc out=$out)"
fi

# Case J2: Bash read-only command over the same file -> exit 0; no write
# shape in the command, so the gate never even runs.
export ENFORCEMENT=blocking GT_VERIFY_EXIT=0
setup_guard_repo
out=$(run_bash_hook "rg foo app/billing.py" 2>&1); rc=$?
if [ $rc -eq 0 ] && [ -z "$out" ]; then
  pass "J2: Bash read-only command -> exit 0"
else
  fail "J2: Bash read-only command -> exit 0 (rc=$rc out=$out)"
fi

# Case J3: Bash sed -i in place on an uncovered file -> exit 2.
export ENFORCEMENT=blocking GT_VERIFY_EXIT=0
setup_guard_repo
out=$(run_bash_hook "sed -i 's/a/b/' app/billing.py" 2>&1); rc=$?
if [ $rc -eq 2 ] && printf '%s' "$out" | grep -q "no claim covers it"; then
  pass "J3: Bash sed -i on uncovered file + blocking -> exit 2"
else
  fail "J3: Bash sed -i on uncovered file + blocking -> exit 2 (rc=$rc out=$out)"
fi

# Case J4: Bash write to a path outside the coverage roots -> exit 0.
export ENFORCEMENT=blocking GT_VERIFY_EXIT=0
setup_guard_repo
out=$(run_bash_hook "cat > README.md" 2>&1); rc=$?
if [ $rc -eq 0 ] && [ -z "$out" ]; then
  pass "J4: Bash write outside coverage roots -> exit 0"
else
  fail "J4: Bash write outside coverage roots -> exit 0 (rc=$rc out=$out)"
fi

# Case S1: three consecutive red stops in blocking mode -> the third
# carries an ESCALATE line telling the session to stop patching.
export ENFORCEMENT=blocking GT_VERIFY_EXIT=1
setup_repo
add_gt_hook_state
dirty_repo
run_stop_hook false >/dev/null 2>&1
run_stop_hook false >/dev/null 2>&1
out=$(run_stop_hook false 2>&1); rc=$?
if [ $rc -eq 2 ] && printf '%s' "$out" | grep -q "ESCALATE: STATUS.yaml still red after 3 sync rounds"; then
  pass "S1: three consecutive red stops in blocking mode -> ESCALATE"
else
  fail "S1: three consecutive red stops in blocking mode -> ESCALATE (rc=$rc out=$out)"
fi

# Case S2: a green stop resets the red counter -- the next red stop after
# it starts counting from 1, not from where the streak left off.
export ENFORCEMENT=blocking GT_VERIFY_EXIT=1
setup_repo
add_gt_hook_state
dirty_repo
run_stop_hook false >/dev/null 2>&1
run_stop_hook false >/dev/null 2>&1
export GT_VERIFY_EXIT=0
out=$(run_stop_hook false 2>&1); rc=$?
export GT_VERIFY_EXIT=1
out2=$(run_stop_hook false 2>&1); rc2=$?
if [ $rc -eq 0 ] && [ $rc2 -eq 2 ] && ! printf '%s' "$out2" | grep -q "ESCALATE"; then
  pass "S2: a green stop resets the red counter"
else
  fail "S2: a green stop resets the red counter (rc=$rc rc2=$rc2 out=$out out2=$out2)"
fi

# Case S3: advisory mode never escalates, no matter how many red stops
# happen in a row -- escalation is a blocking-only concept.
export ENFORCEMENT=advisory GT_VERIFY_EXIT=1
setup_repo
add_gt_hook_state
dirty_repo
run_stop_hook false >/dev/null 2>&1
run_stop_hook false >/dev/null 2>&1
out=$(run_stop_hook false 2>&1); rc=$?
if [ $rc -eq 0 ] && ! printf '%s' "$out" | grep -q "ESCALATE"; then
  pass "S3: advisory mode never escalates, even after repeated red stops"
else
  fail "S3: advisory mode never escalates, even after repeated red stops (rc=$rc out=$out)"
fi

state_has_key() {
  # True if $REPO/.git/ground-truth-state.json exists and its sessions map
  # has the given key -- "" included, since a JSON key is always a string.
  python3 -c "
import json, sys
try:
    data = json.load(open('$REPO/.git/ground-truth-state.json'))
except (OSError, ValueError):
    sys.exit(1)
sys.exit(0 if '$1' in (data.get('sessions') or {}) else 1)
"
}

# Case S4: a Stop event whose stdin carries an empty session_id must not
# key the red-stop counter on a shared "" session -- the hook normalizes
# it the same way a missing session_id already reads, as "unknown".
export ENFORCEMENT=blocking GT_VERIFY_EXIT=1
setup_repo
add_gt_hook_state
dirty_repo
input=$(printf '{"session_id":"","cwd":"%s","hook_event_name":"Stop","stop_hook_active":false}' "$REPO")
printf '%s' "$input" | bash "$REPO/.claude/hooks/ground-truth-sync-check.sh" >/dev/null 2>&1  # gt-allow: real install path, not prose
if ! state_has_key ""; then
  pass "S4: empty session_id on stdin -> no \"\" key in ground-truth-state.json"
else
  fail "S4: empty session_id on stdin -> no \"\" key in ground-truth-state.json ($(cat "$REPO/.git/ground-truth-state.json" 2>/dev/null))"
fi

# Case S5: gt_hook_state.py's own session functions refuse an empty sid
# directly, independent of what any caller normalizes it to first.
export ENFORCEMENT=blocking GT_VERIFY_EXIT=1
setup_repo
add_gt_hook_state
python3 "$REPO/tools/ground_truth/gt_hook_state.py" --root "$REPO" session-start ""  # gt-allow: real install path, not prose
python3 "$REPO/tools/ground_truth/gt_hook_state.py" --root "$REPO" stop-red "" >/dev/null  # gt-allow: real install path, not prose
if ! state_has_key ""; then
  pass "S5: gt_hook_state.py session-start/stop-red with an empty sid write nothing"
else
  fail "S5: gt_hook_state.py session-start/stop-red with an empty sid write nothing ($(cat "$REPO/.git/ground-truth-state.json" 2>/dev/null))"
fi

# Case R1: STATUS.yaml citation whose file gained lines above it -- a
# line-reference drift is a warning, not a red Stop (D70): the hook
# stays green and names the fix in stderr; --apply then clears the
# citation.
export ENFORCEMENT=blocking GT_VERIFY_EXIT=0
setup_remap_repo
shift_pricing_lines
out=$(run_stop_hook false 2>&1); rc=$?
if [ $rc -eq 0 ] && printf '%s' "$out" | grep -q "\[WARN\]" && printf '%s' "$out" | grep -q "remap_line_refs.py --apply"; then
  pass "R1: Stop hook stays green on a stale line reference, warns and names remap --apply"
else
  fail "R1: Stop hook stays green on a stale line reference, warns and names remap --apply (rc=$rc out=$out)"
fi
(cd "$REPO" && python3 tools/ground_truth/remap_line_refs.py --apply --base HEAD STATUS.yaml >/dev/null 2>&1); rc2=$?  # gt-allow: real install path, not prose
if [ $rc2 -eq 0 ]; then
  pass "R1: --apply fixes the stale citation -> exit 0"
else
  fail "R1: --apply fixes the stale citation -> exit 0 (rc2=$rc2)"
fi
out3=$(run_stop_hook false 2>&1); rc3=$?
if [ $rc3 -eq 0 ]; then
  pass "R1: Stop hook re-run after --apply -> exit 0"
else
  fail "R1: Stop hook re-run after --apply -> exit 0 (rc3=$rc3 out3=$out3)"
fi

# Case R4: --apply a second time against the same base -- the citation
# is now on a line that already differs from base (--apply rewrote it),
# so it is worktree coordinates already: nothing left to rewrite, the
# bytes come out identical, and the Stop hook stays green.
export ENFORCEMENT=blocking GT_VERIFY_EXIT=0
setup_remap_repo
shift_pricing_lines
(cd "$REPO" && python3 tools/ground_truth/remap_line_refs.py --apply --base HEAD STATUS.yaml >/dev/null 2>&1)  # gt-allow: real install path, not prose
cp "$REPO/STATUS.yaml" "$WORKDIR/status.after1"
(cd "$REPO" && python3 tools/ground_truth/remap_line_refs.py --apply --base HEAD STATUS.yaml >/dev/null 2>&1); rc=$?  # gt-allow: real install path, not prose
if [ $rc -eq 0 ] && cmp -s "$WORKDIR/status.after1" "$REPO/STATUS.yaml"; then
  pass "R4: --apply twice -> STATUS.yaml bytes unchanged the second time"
else
  fail "R4: --apply twice -> STATUS.yaml bytes unchanged the second time (rc=$rc)"
fi
out=$(run_stop_hook false 2>&1); rc2=$?
if [ $rc2 -eq 0 ]; then
  pass "R4: Stop hook still green after the second --apply"
else
  fail "R4: Stop hook still green after the second --apply (rc2=$rc2 out=$out)"
fi

# Case R2: nothing stale -> exit 0, no fix: hint is printed (a clean
# result must not nudge the session toward --apply), and --check never
# writes the JSON report (only --dry-run/--apply do).
export ENFORCEMENT=advisory GT_VERIFY_EXIT=0
setup_remap_repo
out=$(run_remap_check 2>&1); rc=$?
if [ $rc -eq 0 ] && ! printf '%s' "$out" | grep -q "fix:" && [ ! -f "$REPO/remap_line_refs.report.json" ]; then
  pass "R2: remap --check on a clean repo -> exit 0, no fix: hint, no report file"
else
  fail "R2: remap --check on a clean repo -> exit 0, no fix: hint, no report file (rc=$rc out=$out)"
fi

# Case R3: remap --check crashes while reading an undecodable STATUS.yaml
# (a raw non-UTF-8 byte -- the file is opened with encoding="utf-8" and
# only OSError is caught around that read, so a bad byte raises
# UnicodeDecodeError uncaught) -- the crash must land on exit 2 (a tool
# error), never on exit 1 ("stale references found"), because the Stop
# hook only ever downgrades rc=2 to a warning and treats rc=1 as red.
export ENFORCEMENT=blocking GT_VERIFY_EXIT=0
setup_remap_repo
printf '\xe9' >> "$REPO/STATUS.yaml"
out=$(run_remap_check 2>&1); rc=$?
if [ $rc -eq 2 ] && printf '%s' "$out" | grep -q "^error:"; then
  pass "R3: remap --check on an undecodable STATUS.yaml -> exit 2"
else
  fail "R3: remap --check on an undecodable STATUS.yaml -> exit 2 (rc=$rc out=$out)"
fi
out2=$(run_stop_hook false 2>&1); rc2=$?
if [ $rc2 -eq 0 ]; then
  pass "R3: Stop hook stays green when remap --check errors (rc=2 downgraded to a warning)"
else
  fail "R3: Stop hook stays green when remap --check errors (rc=2 downgraded to a warning) (rc2=$rc2 out2=$out2)"
fi

# Case C1: the SessionStart card names the live enforcement mode and the
# scoped evidence and opt-in edit gates, and
# recording the session actually lands the sid in the state file.
export ENFORCEMENT=blocking GT_VERIFY_EXIT=0
setup_repo
add_gt_hook_state
out=$(run_session_start_hook); rc=$?
python3 -c "import json,sys; d=json.load(open('$REPO/.git/ground-truth-state.json')); sys.exit(0 if 'x' in d.get('sessions', {}) else 1)" 2>/dev/null
state_rc=$?
if [ $rc -eq 0 ] \
   && printf '%s' "$out" | grep -q "## ground-truth contract active (enforcement: blocking)" \
   && printf '%s' "$out" | grep -q "only its declared scope" \
   && printf '%s' "$out" | grep -q "Pre-edit gates are opt-in" \
   && printf '%s' "$out" | grep -q "Skipped or incomplete checks are not proof" \
   && [ $state_rc -eq 0 ]; then
  pass "C1: SessionStart card names the enforcement mode and evidence limits, records the session"
else
  fail "C1: SessionStart card names the enforcement mode and evidence limits, records the session (rc=$rc out=$out state_rc=$state_rc)"
fi

# Case C2: SessionStart resets a prior session's blast acks, observed
# directly through the state CLI (not through the edit guard, unlike E3).
export ENFORCEMENT=blocking GT_VERIFY_EXIT=0
setup_repo
add_gt_hook_state
python3 "$REPO/tools/ground_truth/gt_hook_state.py" --root "$REPO" blast-ack app/pricing.py  # gt-allow: real install path, not prose
python3 "$REPO/tools/ground_truth/gt_hook_state.py" --root "$REPO" blast-ok x app/pricing.py  # gt-allow: real install path, not prose
rc_before=$?
sleep 1
run_session_start_hook >/dev/null
python3 "$REPO/tools/ground_truth/gt_hook_state.py" --root "$REPO" blast-ok x app/pricing.py  # gt-allow: real install path, not prose
rc_after=$?
if [ $rc_before -eq 0 ] && [ $rc_after -eq 1 ]; then
  pass "C2: SessionStart resets a prior session's blast acks"
else
  fail "C2: SessionStart resets a prior session's blast acks (rc_before=$rc_before rc_after=$rc_after)"
fi

# Case C3: a green verifier can still carry sync warnings (D70); the
# Stop hook only prints those on stderr when it fails, so SessionStart
# is the only channel that surfaces them on an otherwise clean session.
export ENFORCEMENT=advisory GT_VERIFY_EXIT=0
setup_repo
out=$(GT_VERIFY_WARN=1 run_session_start_hook); rc=$?
if [ $rc -eq 0 ] \
   && printf '%s' "$out" | grep -q "warnings in STATUS.yaml" \
   && printf '%s' "$out" | grep -q "\[WARN\]"; then
  pass "C3: SessionStart surfaces sync warnings on a green verifier"
else
  fail "C3: SessionStart surfaces sync warnings on a green verifier (rc=$rc out=$out)"
fi

echo "----"
echo "passed: $PASS  failed: $FAIL"
if [ "$FAIL" -ne 0 ]; then
  echo "scratch repo kept for inspection: $REPO"
fi
[ "$FAIL" -eq 0 ]
