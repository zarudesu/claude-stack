#!/usr/bin/env python3
"""Behavioral regressions for proofs, audit and usage; temporary repositories only."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import yaml

sys.dont_write_bytecode = True
SKILL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL / "templates"))
sys.path.insert(0, str(SKILL / "pipeline"))
import contract_lib
import gt_session_guard as guard
from stages import audit_scope, audit_close
from gt_lib.paths import Run
import usage_report

spec = importlib.util.spec_from_file_location("fixture_helpers", SKILL / "selftest/run.py")
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)


class ProcessChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture.build_fixture()
        cls.root = fixture.FIXTURE
        cls.claim = next(c for c in contract_lib.load_contract(cls.root)["claims"]
                         if c["id"] == "alpha_greeting")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(fixture.SCRATCH_BASE)
        shutil.rmtree(fixture.FAKE_MVN_BIN, ignore_errors=True)

    def test_skipped_pytest_is_not_green(self):
        with fixture.patched(self.root / "tests/test_alpha.py", "def test_greet_returns_greeting():",
                             'import pytest\n@pytest.mark.skip(reason="fixture")\ndef test_greet_returns_greeting():'):
            ok, message, skipped = contract_lib.run_claim_check(self.root, self.claim)
            self.assertFalse(ok, "a skipped assertion is not passing evidence")
            self.assertIn("skip", message.lower())

    def test_full_executes_pytest_claims(self):
        with fixture.patched(self.root / "tests/test_alpha.py", '== "hello"', '== "wrong"'):
            data = contract_lib.load_contract(self.root)
            issues = contract_lib.check_collectible(self.root, data, "full")
            self.assertTrue(any(i.severity == "fail" and "pytest" in i.message for i in issues), issues)

    def test_mutation_rejects_already_red_baseline(self):
        with fixture.patched(self.root / "tests/test_alpha.py", '== "hello"', '== "wrong"'):
            result = fixture.mutation_selfcheck("alpha_greeting")
            self.assertEqual(result.returncode, 1, fixture.out(result))
            self.assertIn("baseline", fixture.out(result))

    def test_guard_mutation_rejects_already_red_baseline(self):
        with fixture.patched(self.root / "tests/test_alpha.py", '== "hello"', '== "wrong"'):
            proven, message, _ = guard._mutate_and_run(self.root, self.claim, self.claim["mutation"])
            self.assertFalse(proven)
            self.assertIn("baseline", message)

    def test_ci_refuses_incomplete_check_budget(self):
        args = argparse.Namespace(root=self.root, mode="ci", base="HEAD", max_checks=0, budget_seconds=60)
        with mock.patch.object(guard, "_change_set", return_value={"tests/test_alpha.py"}):
            self.assertEqual(guard.guard(args), 1)

    def test_ci_refuses_checker_error(self):
        self.assertEqual(guard.main(["--mode", "ci", "--root", str(self.root / "missing")]), 1)

    def test_ci_refuses_unreadable_baseline_claims(self):
        with mock.patch.object(contract_lib, "_baseline_status_claims", return_value=None):
            self.assertEqual(guard.main(["--mode", "ci", "--root", str(self.root), "--base", "HEAD"]), 1)

    def test_initial_contract_is_not_silently_exempt_from_proof(self):
        def git_result(root, args):
            if args[0] == "show":
                return subprocess.CompletedProcess(args, 128, "", "path missing")
            return subprocess.CompletedProcess(args, 0, "", "")
        with mock.patch.object(contract_lib, "_git_run", side_effect=git_result):
            baseline = guard._baseline_claims(self.root, self.root, "HEAD")
        self.assertEqual(baseline, {})
        unproven = {**self.claim}
        unproven.pop("mutation", None)
        issues = guard.rule_new_claims(self.root, [unproven], baseline, guard.Budget(None, 60))
        self.assertTrue(any(severity == "fail" for severity, _, _ in issues), issues)

    def test_hook_defers_mutation_without_touching_workspace(self):
        with mock.patch.object(guard, "_mutate_and_run") as mutate:
            issues = guard.rule_new_claims(self.root, [self.claim], {}, guard.Budget(None, 60),
                                          prove_mutations=False)
            mutate.assert_not_called()
        self.assertTrue(any(severity == "warn" and "pending" in message
                            for severity, _, message in issues), issues)

    def test_skill_entry_yaml_and_local_links(self):
        text = (SKILL / "SKILL.md").read_text()
        frontmatter = yaml.safe_load(text.split("---", 2)[1])
        self.assertEqual(frontmatter["name"], "ground-truth")
        self.assertIs(frontmatter["disable-model-invocation"], True)
        self.assertLess(len(frontmatter["description"]), 1024)
        for target in re.findall(r"\]\(([^)]+)\)", text):
            if "://" not in target:
                self.assertTrue((SKILL / target.split("#", 1)[0]).is_file(), target)

    @unittest.skipUnless(shutil.which("go"), "Go toolchain unavailable")
    def test_go_runtime_skip_is_not_green(self):
        with tempfile.TemporaryDirectory(prefix="gt-go-skip-") as temp:
            root = Path(temp)
            with fixture.added_file(root / "go.mod", "module example.com/gtfixture\n\ngo 1.20\n"), fixture.added_file(
                root / "skip_test.go",
                'package gtfixture\nimport "testing"\nfunc TestSkip(t *testing.T) { t.Skip("fixture") }\n'):
                ok, message, skipped = contract_lib.run_claim_check(
                    root, {"check_kind": "go_test", "check": ".::TestSkip"})
                self.assertFalse(ok)
                self.assertTrue(skipped, message)

    @unittest.skipUnless(shutil.which("node"), "Node toolchain unavailable")
    def test_node_runtime_skip_is_not_green(self):
        with tempfile.TemporaryDirectory(prefix="gt-node-skip-") as temp:
            root = Path(temp)
            with fixture.added_file(root / "skip.test.cjs",
                'const test = require("node:test");\ntest("skip me", t => { t.skip("fixture"); });\n'):
                ok, message, skipped = contract_lib.run_claim_check(
                    root, {"check_kind": "js_test", "check": "skip.test.cjs::skip me"})
                self.assertFalse(ok)
                self.assertTrue(skipped, message)

    def test_js_report_requires_passing_selected_assertion(self):
        with tempfile.TemporaryDirectory(prefix="gt-js-report-") as temp:
            for state, expected in [("passed", True), ("pending", False), ("failed", False)]:
                script = ("import json,pathlib,sys; "
                          "p=next(a.split('=',1)[1] for a in sys.argv if a.startswith('--outputFile=')); "
                          "pathlib.Path(p).write_text(json.dumps({'testResults':[{'assertionResults':["
                          f"{{'title':'fixture','status':{state!r}}}"
                          "]}]}))")
                ok, message, skipped = contract_lib._run_reported(
                    Path(temp), [sys.executable, "-c", script], dict(os.environ), 10, "jest", "fixture")
                self.assertEqual(ok, expected, message)

    def test_symbol_name_heuristic_is_advisory(self):
        issues = guard.rule_new_definitions(self.root, [], {"services/new.py"},
                                           {"services/new.py": ["def wrapper():"]}, ["services"], [])
        self.assertTrue(issues)
        self.assertTrue(all(i[0] == "warn" for i in issues), issues)

    def test_preedit_gate_is_optional(self):
        env = dict(os.environ)
        env.pop("GT_STRICT_EDIT_GUARD", None)
        event = {"cwd": str(self.root), "session_id": "process-check", "tool_name": "Edit",
                 "tool_input": {"file_path": str(self.root / "services/alpha/core.py")}}
        result = subprocess.run(["bash", str(SKILL / "templates/ground-truth-edit-guard.sh")],
                                input=json.dumps(event), text=True, capture_output=True,
                                cwd=self.root, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_audit_uses_reviewed_commit_not_last_sync(self):
        reviewed = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=self.root, text=True).strip()
        base, reason = audit_scope.audit_base(self.root, {"last_audited_commit": reviewed})
        self.assertEqual(base, reviewed)
        self.assertEqual(reason, "")
        missing, reason = audit_scope.audit_base(self.root, {})
        self.assertIsNone(missing)
        self.assertTrue(reason)

    def test_root_dot_drift_includes_committed_and_untracked(self):
        with fixture.added_file(self.root / "services/extra.py", "VALUE = 1\n"):
            self.assertIn("services/extra.py", audit_scope.drift_files(self.root, "HEAD", ["."]))

    def run_config(self, scratch):
        return Run(repo=self.root, research=scratch, scratch=scratch,
                   python=Path(sys.executable), skill=SKILL, date=fixture.TODAY)

    def test_clean_audit_does_not_refresh_date_or_schedule_fleet(self):
        head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=self.root, text=True).strip()
        with tempfile.TemporaryDirectory(prefix="gt-audit-test-") as temp, fixture.patched(
                self.root / "STATUS.yaml", f'last_audited: "{fixture.TODAY}"',
                f'last_audited: "{fixture.TODAY}"\n  last_audited_commit: "{head}"'):
            before = (self.root / "STATUS.yaml").read_bytes()
            run = self.run_config(Path(temp))
            with mock.patch.object(audit_scope, "load_run", return_value=run):
                self.assertEqual(audit_scope.main(["--run", "fixture"]), 0)
            report = json.loads((run.scratch / "audit/audit-scope.json").read_text())
            self.assertTrue(report["short_circuit"], report)
            self.assertEqual(report["sample_claims"], [])
            self.assertEqual((self.root / "STATUS.yaml").read_bytes(), before)
            self.assertFalse(any("i10-fleet" in s for s in report["next"]))

    def test_claim_only_change_is_audit_work(self):
        head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=self.root, text=True).strip()
        with tempfile.TemporaryDirectory(prefix="gt-audit-test-") as temp, fixture.patched(
                self.root / "STATUS.yaml", f'last_audited: "{fixture.TODAY}"',
                f'last_audited: "{fixture.TODAY}"\n  last_audited_commit: "{head}"'), fixture.patched(
                self.root / "STATUS.yaml", self.claim["note"], self.claim["note"] + " Revised behavior."):
            run = self.run_config(Path(temp))
            with mock.patch.object(audit_scope, "load_run", return_value=run):
                self.assertEqual(audit_scope.main(["--run", "fixture", "--skip-verify"]), 0)
            report = json.loads((run.scratch / "audit/audit-scope.json").read_text())
            self.assertIn("alpha_greeting", report["touched_claims"])
            self.assertIn("alpha_greeting", report["changed_claims"])
            self.assertFalse(report["short_circuit"])

    def test_audit_close_requires_review_even_with_force(self):
        self.assertEqual(audit_close.main(["--run", "unused", "--force", "--write"]), 1)

    def test_audit_close_rechecks_current_tests_and_preserves_failed_contract(self):
        with tempfile.TemporaryDirectory(prefix="gt-audit-test-") as temp, fixture.patched(
                self.root / "tests/test_alpha.py", '== "hello"', '== "wrong"'):
            before = (self.root / "STATUS.yaml").read_bytes()
            run = self.run_config(Path(temp))
            with mock.patch.object(audit_close, "load_run", return_value=run):
                self.assertEqual(audit_close.main(
                    ["--run", "fixture", "--force", "--reviewed", "--write"]), 1)
            self.assertEqual((self.root / "STATUS.yaml").read_bytes(), before)

    def test_usage_deduplicates_and_prefers_final_result(self):
        message = {"type": "assistant", "message": {"id": "a", "usage": {
            "input_tokens": 100, "cache_read_input_tokens": 40, "output_tokens": 10}}}
        report = usage_report.analyze([message, message])
        self.assertEqual(report["tokens"]["input_uncached"], 100)
        self.assertIsNone(report["reported_cost_usd"])
        result = {"type": "result", "total_cost_usd": 0.2, "modelUsage": {
            "fixture": {"inputTokens": 101, "cacheReadInputTokens": 41, "outputTokens": 11}}}
        report = usage_report.analyze([message, message, result])
        self.assertEqual(report["tokens"]["input_uncached"], 101)
        self.assertEqual(report["reported_cost_usd"], 0.2)

    def test_usage_codex_cache_and_reasoning_are_not_double_counted(self):
        report = usage_report.analyze([{"payload": {"type": "token_count", "info": {
            "total_token_usage": {"input_tokens": 100, "cached_input_tokens": 80,
                                 "output_tokens": 30, "reasoning_output_tokens": 20}}}}])
        self.assertEqual(report["tokens"], {
            "input_uncached": 20, "input_cache_read": 80, "input_cache_write": 0, "output": 30})
        combined = usage_report.aggregate([report, usage_report.analyze([{"totalTokens": 500}])])
        self.assertIsNone(combined["complete_reported_cost_usd"])
        self.assertEqual(combined["unknown_cost_files"], 2)


if __name__ == "__main__":
    unittest.main()
