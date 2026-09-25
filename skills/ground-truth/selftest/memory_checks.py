#!/usr/bin/env python3
"""Executable project-memory contracts in temporary multi-repository workspaces."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
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
import gt_context as memory


class MemoryChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="gt-memory-test-")
        self.root = Path(self.temp.name)
        (self.root / ".ground-truth").mkdir()
        for name in ("backend", "bot", "landing"):
            repo = self.root / name
            (repo / "src").mkdir(parents=True)
            (repo / "docs").mkdir()
            (repo / "src/core.py").write_text("VALUE = 1\n")
            (repo / "docs/context.md").write_text(f"{name}: canonical behavior and constraints\n")
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo), "-c", "user.name=fixture", "-c",
                            "user.email=fixture@example.invalid", "commit", "-qm", "fixture"], check=True)
        self.model = {"version": 1, "repositories": {name: {"path": name} for name in ("backend", "bot", "landing")},
                      "areas": [{"id": name, "repo": name, "summary": f"{name} route",
                                 "sources": ["src"], "docs": ["docs/context.md"],
                                 "depends_on": ["backend"] if name == "bot" else [],
                                 "entrypoints": ["src/core.py"], "checks": ["appropriate project tests"],
                                 "unknowns": []} for name in ("backend", "bot", "landing")]}
        self.write_model()

    def tearDown(self):
        self.temp.cleanup()

    def write_model(self):
        (self.root / memory.MODEL).write_text(yaml.safe_dump(self.model, sort_keys=False))

    def accept(self, ids=None):
        view = memory.inspect(self.root)
        memory.refresh(self.root, ids or list(view["nodes"]), view["revision"], "fixture semantic review")

    def cli(self, *args):
        with contextlib.redirect_stdout(io.StringIO()) as output, contextlib.redirect_stderr(io.StringIO()):
            rc = memory.main(["--root", str(self.root), *args])
        return rc, output.getvalue()

    def test_initial_model_requires_review(self):
        self.assertFalse(memory.summary(memory.inspect(self.root))["ready"])
        rc, text = self.cli("context", "--area", "bot")
        self.assertEqual(rc, 1)
        self.assertEqual(json.loads(text)["documents"], [])

    def test_context_reuses_canonical_documents_and_known_consumers(self):
        self.accept()
        rc, text = self.cli("context", "--area", "backend")
        self.assertEqual(rc, 0, text)
        packet = json.loads(text)
        self.assertEqual(packet["affected_consumers"], ["bot"])
        self.assertEqual(len(packet["documents"]), 1)
        self.assertNotIn("landing", packet["required_areas"])

    def test_code_change_invalidates_consumer_but_not_unrelated_area(self):
        self.accept()
        (self.root / "backend/src/core.py").write_text("VALUE = 2\n")
        view = memory.inspect(self.root)
        self.assertFalse(view["areas"]["backend"]["fresh"])
        self.assertFalse(view["areas"]["bot"]["fresh"])
        self.assertTrue(view["areas"]["landing"]["fresh"])
        rc, text = self.cli("context", "--area", "bot")
        self.assertEqual(rc, 1)
        self.assertEqual(json.loads(text)["documents"], [])

    def test_document_edit_invalidates_receipt(self):
        self.accept()
        (self.root / "backend/docs/context.md").write_text("Different claim\n")
        self.assertFalse(memory.inspect(self.root)["areas"]["backend"]["fresh"])

    def test_context_names_shared_source_owners_without_loading_their_docs(self):
        (self.root / "backend/docs/tooling.md").write_text("Tooling-specific canonical knowledge\n")
        self.model["areas"].append({"id": "tooling", "repo": "backend", "summary": "shared source owner",
                                    "sources": ["src/core.py"], "docs": ["docs/tooling.md"]})
        self.write_model()
        self.accept()
        rc, text = self.cli("context", "--area", "backend")
        self.assertEqual(rc, 0, text)
        packet = json.loads(text)
        self.assertEqual(packet["co_owned_areas"], ["tooling"])
        self.assertIn("tooling", [r["id"] for r in packet["routes"]])
        self.assertNotIn("docs/tooling.md", [d["path"] for d in packet["documents"]])
        (self.root / "backend/src/core.py").write_text("VALUE = 55\n")
        self.assertFalse(memory.inspect(self.root)["areas"]["tooling"]["fresh"])

    def test_new_file_in_known_area_is_detected_without_committing(self):
        self.accept()
        (self.root / "backend/src/new.py").write_text("NEW = 2\n")
        view = memory.inspect(self.root)
        self.assertFalse(view["areas"]["backend"]["fresh"])
        self.assertIn("src/new.py", view["areas"]["backend"]["changed_files"])

    def test_explicit_safe_ignored_config_is_watched_but_ignored_tree_is_not_swept(self):
        repo = self.root / "backend"
        (repo / ".git/info/exclude").write_text("local-settings.json\nlocal-secret.env\n")
        (repo / "local-settings.json").write_text('{"hook": "enabled"}\n')
        (repo / "local-secret.env").write_text("fixture secret placeholder\n")
        self.model["areas"][0]["sources"].append("local-settings.json")
        self.write_model()
        self.accept()
        view = memory.inspect(self.root)
        self.assertIn("local-settings.json", view["areas"]["backend"]["inputs"])
        self.assertNotIn("local-secret.env", view["areas"]["backend"]["inputs"])
        (repo / "local-settings.json").write_text('{"hook": "disabled"}\n')
        self.assertFalse(memory.inspect(self.root)["areas"]["bot"]["fresh"])
        (repo / "local-settings.json").unlink()
        self.assertTrue(memory.inspect(self.root)["areas"]["backend"]["errors"])

    def test_new_unclassified_area_refuses_global_acceptance(self):
        self.accept()
        (self.root / "backend/new-config.yaml").write_text("setting: true\n")
        view = memory.inspect(self.root)
        self.assertIn("new-config.yaml", view["gaps"]["backend"])
        with self.assertRaises(ValueError):
            memory.refresh(self.root, ["backend"], view["revision"], "cannot silently ignore a new input")

    def test_deleted_source_is_not_silent_success(self):
        self.accept()
        (self.root / "backend/src/core.py").unlink()
        self.assertTrue(memory.inspect(self.root)["areas"]["backend"]["errors"])

    def test_model_dependency_change_requires_review(self):
        self.accept()
        self.model["areas"][2]["depends_on"] = ["backend"]
        self.write_model()
        self.assertFalse(memory.inspect(self.root)["areas"]["landing"]["fresh"])

    def test_refresh_cannot_approve_changes_after_review(self):
        self.accept()
        before = (self.root / memory.RECEIPTS).read_bytes()
        old = memory.inspect(self.root)["revision"]
        (self.root / "backend/src/core.py").write_text("VALUE = 7\n")
        with self.assertRaises(ValueError):
            memory.refresh(self.root, ["backend"], old, "review of old content")
        self.assertEqual((self.root / memory.RECEIPTS).read_bytes(), before)

    def test_refresh_is_local_not_blanket_approval(self):
        self.accept()
        (self.root / "backend/src/core.py").write_text("VALUE = 7\n")
        (self.root / "landing/src/core.py").write_text("VALUE = 8\n")
        self.accept(["backend"])
        view = memory.inspect(self.root)
        self.assertTrue(view["areas"]["backend"]["fresh"])
        self.assertFalse(view["areas"]["bot"]["fresh"])
        self.assertFalse(view["areas"]["landing"]["fresh"])

    def test_missing_repository_blocks_affected_packet(self):
        self.accept()
        shutil.rmtree(self.root / "backend")
        self.assertEqual(self.cli("context", "--area", "bot")[0], 1)
        self.assertEqual(self.cli("context", "--area", "landing")[0], 0)
        self.assertEqual(self.cli("check")[0], 1)

    def test_cycle_is_finite_and_propagates_drift(self):
        self.model["areas"][0]["depends_on"] = ["bot"]
        self.write_model()
        self.accept()
        (self.root / "bot/src/core.py").write_text("VALUE = 4\n")
        view = memory.inspect(self.root)
        self.assertFalse(view["areas"]["backend"]["fresh"])
        self.assertFalse(view["areas"]["bot"]["fresh"])

    def test_document_budget_never_silently_truncates(self):
        self.accept()
        rc, text = self.cli("context", "--area", "bot", "--max-chars", "10")
        self.assertEqual(rc, 1)
        self.assertFalse(json.loads(text)["packet_complete"])

    def test_expired_external_evidence_cannot_be_refreshed(self):
        self.model["areas"][0]["valid_until"] = "2020-01-01T00:00:00Z"
        self.write_model()
        with self.assertRaises(ValueError):
            self.accept()

    def test_unknown_dependency_rejected(self):
        self.model["areas"][0]["depends_on"] = ["phantom"]
        self.write_model()
        with self.assertRaises(ValueError):
            memory.inspect(self.root)

    def test_status_and_context_do_not_write_receipts(self):
        self.accept()
        before = (self.root / memory.RECEIPTS).read_bytes()
        self.cli("status")
        self.cli("context", "--area", "bot")
        self.assertEqual((self.root / memory.RECEIPTS).read_bytes(), before)

    def test_symlink_outside_declared_repository_is_refused(self):
        (self.root / "backend/src/external.py").symlink_to(self.root / "bot/src/core.py")
        self.assertTrue(memory.inspect(self.root)["areas"]["backend"]["errors"])

    def test_missing_legacy_model_is_not_ready(self):
        (self.root / memory.MODEL).unlink()
        self.assertEqual(self.cli("check")[0], 2)

    def test_missing_document_is_not_ready(self):
        self.accept()
        (self.root / "bot/docs/context.md").unlink()
        self.assertEqual(self.cli("context", "--area", "bot")[0], 1)

    def test_committed_change_is_still_stale(self):
        self.accept()
        repo = self.root / "backend"
        (repo / "src/core.py").write_text("VALUE = 9\n")
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(repo), "-c", "user.name=fixture", "-c",
                        "user.email=fixture@example.invalid", "commit", "-qm", "external edit"], check=True)
        self.assertEqual(memory.git(repo, "status", "--porcelain"), "")
        self.assertEqual(self.cli("context", "--area", "bot")[0], 1)

    def test_removed_area_requires_explicit_pruning(self):
        self.accept()
        self.model["areas"] = self.model["areas"][:2]
        del self.model["repositories"]["landing"]
        self.write_model()
        view = memory.inspect(self.root)
        self.assertEqual(view["removed_areas"], ["landing"])
        self.assertEqual(self.cli("check")[0], 1)
        memory.refresh(self.root, ["backend"], view["revision"], "reviewed area retirement", prune=True)
        self.assertEqual(self.cli("check")[0], 0)

    def test_concurrent_refresh_does_not_overwrite_receipt(self):
        self.accept()
        before = (self.root / memory.RECEIPTS).read_bytes()
        lock = self.root / (memory.RECEIPTS + ".lock")
        lock.write_text("another reviewer")
        with self.assertRaises(FileExistsError):
            self.accept(["backend"])
        self.assertEqual((self.root / memory.RECEIPTS).read_bytes(), before)
        self.assertEqual(lock.read_text(), "another reviewer")

    def test_malformed_receipt_fails_closed(self):
        self.accept()
        state = json.loads((self.root / memory.RECEIPTS).read_text())
        state["areas"]["backend"]["inputs"] = ["not a mapping"]
        (self.root / memory.RECEIPTS).write_text(json.dumps(state))
        self.assertEqual(self.cli("check")[0], 2)

    def test_timestamp_without_timezone_is_rejected(self):
        self.model["areas"][0]["valid_until"] = "2099-01-01T00:00:00"
        self.write_model()
        self.assertEqual(self.cli("check")[0], 2)

    def test_advisory_hook_cannot_turn_ci_green(self):
        self.assertEqual(self.cli("hook", "--event", "stop")[0], 0)
        self.assertEqual(self.cli("check")[0], 1)
        self.assertFalse((self.root / memory.RECEIPTS).exists())

    def test_blocking_hook_does_not_approve_stale(self):
        self.model["enforcement"] = "blocking"
        self.write_model()
        self.assertEqual(self.cli("hook", "--event", "start")[0], 0)
        self.assertEqual(self.cli("hook", "--event", "stop")[0], 2)
        self.accept()
        self.assertEqual(self.cli("hook", "--event", "stop")[0], 0)

    def test_context_change_during_packet_output_is_not_green(self):
        self.accept()
        original = memory.packet
        def changed(view, selected, max_chars):
            out = original(view, selected, max_chars)
            (self.root / "backend/src/core.py").write_text("VALUE = 111\n")
            return out
        with mock.patch.object(memory, "packet", side_effect=changed):
            rc, text = self.cli("context", "--area", "bot")
        self.assertEqual(rc, 1)
        self.assertEqual(json.loads(text)["documents"], [])

    def test_document_changed_between_hash_and_read_is_not_served(self):
        self.accept()
        view = memory.inspect(self.root)
        (self.root / "backend/docs/context.md").write_text("changed during packet\n")
        with self.assertRaises(ValueError):
            memory.packet(view, ["backend"], 24000)

    def test_workspace_pointer_finds_shared_model(self):
        pointer = self.root / "bot/.ground-truth/workspace"
        pointer.parent.mkdir()
        pointer.write_text("..\n")
        self.assertEqual(memory.discover(self.root / "bot/src"), self.root.resolve())

    def test_other_worktree_cannot_borrow_canonical_receipts(self):
        self.accept()
        task_tree = self.root / "backend-task"
        subprocess.run(["git", "-C", str(self.root / "backend"), "worktree", "add", "-q", "--detach", str(task_tree)], check=True)
        with self.assertRaisesRegex(ValueError, "active worktree"):
            memory.bind_checkout(memory.inspect(self.root), task_tree)
        memory.bind_checkout(memory.inspect(self.root), self.root / "backend")

    def test_rebound_identical_worktree_reuses_content_review(self):
        self.accept()
        task_tree = self.root / "backend-task"
        subprocess.run(["git", "-C", str(self.root / "backend"), "worktree", "add", "-q", "--detach", str(task_tree)], check=True)
        self.model["repositories"]["backend"]["path"] = "backend-task"
        self.write_model()
        view = memory.inspect(self.root)
        memory.bind_checkout(view, task_tree)
        self.assertTrue(memory.summary(view)["ready"])
        self.assertEqual(memory.packet(view, ["backend"], 24000)["routes"][0]["repository_path"], str(task_tree.resolve()))
        (task_tree / "src/core.py").write_text("VALUE = 42\n")
        self.assertFalse(memory.inspect(self.root)["areas"]["bot"]["fresh"])

    def install_hook_helper(self):
        dest = self.root / "bot/tools/ground_truth"
        dest.mkdir(parents=True)
        shutil.copyfile(SKILL / "templates/gt_context.py", dest / "gt_context.py")
        self.model["areas"][1]["sources"].append("tools")
        self.model["enforcement"] = "blocking"
        self.write_model()

    def hook(self, name):
        return subprocess.run(["bash", str(SKILL / "templates" / name)],
                              input=json.dumps({"cwd": str(self.root / "bot"), "session_id": "fixture"}),
                              text=True, capture_output=True, env={**os.environ, "GT_PYTHON": sys.executable})

    def test_stop_checks_sibling_drift_without_status_contract(self):
        self.install_hook_helper()
        self.accept()
        self.assertEqual(self.hook("ground-truth-sync-check.sh").returncode, 0)
        before = (self.root / memory.RECEIPTS).read_bytes()
        (self.root / "backend/src/core.py").write_text("VALUE = 99\n")
        proc = self.hook("ground-truth-sync-check.sh")
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("stale=backend,bot", proc.stderr)
        self.assertEqual((self.root / memory.RECEIPTS).read_bytes(), before)

    def test_session_start_warns_about_sibling_drift(self):
        self.install_hook_helper()
        self.accept()
        (self.root / "backend/src/core.py").write_text("VALUE = 99\n")
        proc = self.hook("ground-truth-session-start.sh")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("ready=False", proc.stdout)

    def test_installer_and_ci_share_real_memory_helper(self):
        sys.path.insert(0, str(SKILL / "pipeline"))
        from stages import i9_install
        self.assertIn("gt_context.py", i9_install.TOOLS_FILES)
        old = "jobs:\n  verify-status-contract:\n    steps:\n      - run: python3 tools/ground_truth/verify.py --mode=full\n"
        updated = i9_install._ci_add_commands(old)
        parsed = yaml.safe_load(updated)
        run = parsed["jobs"]["verify-status-contract"]["steps"][0]["run"]
        self.assertIn("python3 tools/ground_truth/gt_context.py check", run)
        self.assertEqual(i9_install._ci_add_commands(updated), updated)

    def test_installed_assets_do_not_fail_their_own_full_scanner(self):
        sys.path.insert(0, str(SKILL / "pipeline"))
        from stages import i9_install
        import contract_lib
        repo = self.root / "bot"
        installed = repo / "tools/ground_truth"
        installed.mkdir(parents=True)
        for name in i9_install.TOOLS_FILES + i9_install.HOOK_FILES:
            shutil.copyfile(SKILL / "templates" / name, installed / name)
        shutil.copyfile(SKILL / "templates/ground-truth.rule.md", repo / "RULE.md")
        failures = [i for i in contract_lib.check_no_banned_words(repo, {}) if i.severity == "fail"]
        self.assertEqual(failures, [], failures)
        hook = installed / "ground-truth-session-start.sh"
        clean = hook.read_text()
        hook.write_text(clean + "\n# No LLM calls.\n")  # gt-allow: regression payload for the installed hook scanner
        self.assertTrue(any(i.severity == "fail" for i in contract_lib.check_no_banned_words(repo, {})))
        hook.write_text(clean)
        self.assertFalse(any(i.severity == "fail" for i in contract_lib.check_no_banned_words(repo, {})))


if __name__ == "__main__":
    unittest.main()
