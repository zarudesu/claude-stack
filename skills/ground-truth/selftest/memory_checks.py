#!/usr/bin/env python3
"""Executable project-memory contracts in temporary multi-repository workspaces."""
from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import yaml

sys.dont_write_bytecode = True
SKILL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL / "templates"))
import gt_context as memory
import gt_pending


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

    def test_contract_shards_and_queue_log_are_not_source_drift(self):
        self.accept()
        shards = self.root / "backend/.ground-truth"
        (shards / "areas").mkdir(parents=True)
        for rel, text in (("pending-log.yaml", "[]\n"), ("contract.yaml", "schema_version: 2\n"),
                          ("areas/backend.yaml", "area: backend\nclaims: []\n")):
            (shards / rel).write_text(text)
        view = memory.inspect(self.root)
        self.assertEqual(view["gaps"]["backend"], [])
        self.assertTrue(view["areas"]["backend"]["fresh"])

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
        self.assertEqual(self.cli("hook", "--event", "stop")[0], 0)
        self.assertFalse((self.root / memory.RECEIPTS).exists())
        self.assertIn("stale-area:bot", gt_pending.load(self.root)[2])
        self.accept()
        self.assertEqual(self.cli("hook", "--event", "stop")[0], 0)
        self.assertEqual(gt_pending.load(self.root)[2], {})

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

    def test_context_lists_pending_entries_of_the_area(self):
        self.accept()
        with contextlib.redirect_stderr(io.StringIO()):
            gt_pending.record(self.root, [("stale-area", "bot", "bot", "bot evidence"),
                                          ("stale-area", "landing", "landing", "landing evidence")],
                              gt_pending.MEMORY_CLASSES, "s1")
        rc, text = self.cli("context", "--area", "bot")
        self.assertEqual(rc, 0, text)
        self.assertEqual(json.loads(text)["pending"],
                         [{"key": "stale-area:bot", "evidence": "bot evidence", "escalated": False}])

    def install_hook_helper(self):
        dest = self.root / "bot/tools/ground_truth"
        dest.mkdir(parents=True)
        shutil.copyfile(SKILL / "templates/gt_context.py", dest / "gt_context.py")
        shutil.copyfile(SKILL / "templates/gt_pending.py", dest / "gt_pending.py")
        shutil.copyfile(SKILL / "templates/gt_store.py", dest / "gt_store.py")
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
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("queued 2: stale-area:backend, stale-area:bot", proc.stderr)
        self.assertEqual((self.root / memory.RECEIPTS).read_bytes(), before)

    def test_stop_without_helper_or_on_clean_tree_does_not_block(self):
        (self.root / memory.MODEL).unlink()
        bot = self.root / "bot"
        self.assertEqual(self.hook("ground-truth-sync-check.sh").returncode, 0)
        (bot / "src/core.py").write_text("VALUE = 5\n")
        proc = self.hook("ground-truth-sync-check.sh")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        subprocess.run(["git", "-C", str(bot), "checkout", "-q", "--", "."], check=True)
        dest = bot / "tools/ground_truth"
        dest.mkdir(parents=True)
        shutil.copyfile(SKILL / "templates/gt_context.py", dest / "gt_context.py")
        shutil.copyfile(SKILL / "templates/gt_pending.py", dest / "gt_pending.py")
        shutil.copyfile(SKILL / "templates/gt_store.py", dest / "gt_store.py")
        subprocess.run(["git", "-C", str(bot), "add", "."], check=True)
        subprocess.run(["git", "-C", str(bot), "-c", "user.name=fixture", "-c",
                        "user.email=fixture@example.invalid", "commit", "-qm", "helper"], check=True)
        proc = self.hook("ground-truth-sync-check.sh")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        (bot / "src/core.py").write_text("VALUE = 6\n")
        self.assertEqual(self.hook("ground-truth-sync-check.sh").returncode, 2)

    def test_session_start_warns_about_sibling_drift(self):
        self.install_hook_helper()
        self.accept()
        (self.root / "backend/src/core.py").write_text("VALUE = 99\n")
        proc = self.hook("ground-truth-session-start.sh")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("ready=False", proc.stdout)

    def test_workspace_queue_gates_stop_under_blocking_only(self):
        self.install_hook_helper()
        self.accept()
        (self.root / "backend/src/core.py").write_text("VALUE = 99\n")
        self.assertEqual(self.hook("ground-truth-sync-check.sh").returncode, 0)
        queue = self.root / gt_pending.QUEUE
        data = yaml.safe_load(queue.read_text())
        data["limits"]["per_area"] = 0
        queue.write_text(yaml.safe_dump(data, sort_keys=False))
        proc = self.hook("ground-truth-sync-check.sh")
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("area backend has 1/0 entries", proc.stderr)
        data["limits"]["per_area"] = 5
        data["pending"][0]["first_seen"]["date"] = "2000-01-01"
        queue.write_text(yaml.safe_dump(data, sort_keys=False))
        proc = self.hook("ground-truth-sync-check.sh")
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("stale-area:backend escalated: first seen", proc.stderr)
        self.model["enforcement"] = "advisory"
        self.write_model()
        proc = self.hook("ground-truth-sync-check.sh")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("ground-truth (advisory): pending queue blocks this area", proc.stderr)
        proc = self.hook("ground-truth-session-start.sh")
        self.assertEqual(proc.stdout.count("ground-truth pending 2/30, oldest"), 1, proc.stdout)

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


class PendingQueueChecks(unittest.TestCase):
    """Deferred drift queue behind the real Stop/SessionStart hooks, verify faked."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="gt-pending-test-")
        self.repo = Path(self.temp.name) / "repo"
        self.warns = Path(self.temp.name) / "warns.txt"
        tools = self.repo / "tools/ground_truth"
        tools.mkdir(parents=True)
        shutil.copyfile(SKILL / "templates/gt_pending.py", tools / "gt_pending.py")
        shutil.copyfile(SKILL / "templates/gt_store.py", tools / "gt_store.py")
        (tools / "verify.py").write_text(
            "import os, sys\n"
            "if '--show-warn' in sys.argv:\n"
            "    sys.stdout.write(open(os.environ['GT_FAKE_WARNS']).read())\n")
        (self.repo / "STATUS.yaml").write_text("meta:\n  enforcement: blocking\nclaims: []\n")
        for name in ("src/a.py", "docs/a.md"):
            (self.repo / name).parent.mkdir(exist_ok=True)
            (self.repo / name).write_text("x\n")
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=fixture", "-c",
                        "user.email=fixture@example.invalid", "commit", "-qm", "fixture"], check=True)

    def tearDown(self):
        self.temp.cleanup()

    def warn(self, *paths):
        self.warns.write_text("".join(f"[WARN] - : {p} is not covered by any claim\n" for p in paths))

    def hook(self, name, session="s1", touch="src/a.py", today=None):
        if touch:
            (self.repo / touch).write_text(f"{session}\n")
        return subprocess.run(["bash", str(SKILL / "templates" / name)],
                              input=json.dumps({"cwd": str(self.repo), "session_id": session}),
                              text=True, capture_output=True,
                              env={**os.environ, "GT_PYTHON": sys.executable, "GT_FAKE_WARNS": str(self.warns),
                                   **({"GT_TODAY": today} if today else {})})

    def entries(self):
        return gt_pending.load(self.repo)[2]

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=fixture", "-c",
                               "user.email=fixture@example.invalid", *args], capture_output=True, text=True)

    def edit_entry(self, key, **fields):
        queue = self.repo / gt_pending.QUEUE
        data = yaml.safe_load(queue.read_text())
        next(e for e in data["pending"] if e["key"] == key).update(fields)
        queue.write_text(yaml.safe_dump(data, sort_keys=False))

    def test_deferred_class_is_queued_not_blocking(self):
        self.warn("src/a.py")
        proc = self.hook("ground-truth-sync-check.sh")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("queued 1: uncovered:src/a.py", proc.stderr)
        self.assertEqual(self.entries()["uncovered:src/a.py"]["area"], "src")

    def test_repeat_stop_leaves_file_and_counts_sessions_locally(self):
        self.warn("src/a.py")
        self.hook("ground-truth-sync-check.sh", "s1")
        queue = (self.repo / gt_pending.QUEUE).read_bytes()
        for session in ("s2", "s1"):
            self.hook("ground-truth-sync-check.sh", session)
        self.assertEqual((self.repo / gt_pending.QUEUE).read_bytes(), queue)
        self.assertEqual(set(self.entries()["uncovered:src/a.py"]),
                         {"key", "area", "class", "first_seen", "evidence", "batches_survived",
                          "escalated"})
        local = json.loads((self.repo / ".git" / gt_pending.SESSIONS).read_text())
        self.assertEqual(local, {"uncovered:src/a.py": ["s1", "s2"]})

    def test_branches_of_different_sessions_merge_without_conflict(self):
        self.warn("src/a.py")
        self.hook("ground-truth-sync-check.sh", "s1")
        self.git("add", "-A")
        self.git("commit", "-qm", "queue")
        self.git("branch", "other")
        self.git("checkout", "-qb", "one")
        self.hook("ground-truth-sync-check.sh", "s2")
        self.git("commit", "-qam", "one")
        self.git("checkout", "-q", "other")
        self.hook("ground-truth-sync-check.sh", "s3", touch="docs/a.md")
        self.git("commit", "-qam", "other")
        merge = self.git("merge", "-q", "--no-edit", "one")
        self.assertEqual(merge.returncode, 0, merge.stdout + merge.stderr)
        self.assertEqual(list(self.entries()), ["uncovered:src/a.py"])

    def test_crashing_verify_keeps_the_queue(self):
        self.warn("src/a.py")
        self.hook("ground-truth-sync-check.sh")
        tools = self.repo / "tools/ground_truth"
        shutil.copyfile(SKILL / "templates/verify.py", tools / "verify.py")
        shutil.copyfile(SKILL / "templates/gt_store.py", tools / "gt_store.py")
        (tools / "contract_lib.py").write_text(
            "class ContractError(Exception):\n    pass\n\n\ndef run(root, mode):\n    raise RuntimeError('boom')\n")
        proc = self.hook("ground-truth-sync-check.sh")
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("[FAIL] - : internal error: RuntimeError: boom", proc.stderr)
        self.assertIn("uncovered:src/a.py", self.entries())

    def test_global_entry_does_not_block(self):
        self.warn("top.py")
        self.hook("ground-truth-sync-check.sh", touch="top.py")
        self.edit_entry("uncovered:top.py", first_seen={"date": "2000-01-01", "rev": "-"})
        proc = self.hook("ground-truth-sync-check.sh", touch="top.py")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.entries()["uncovered:top.py"]["area"], "global")

    def test_unreadable_queue_blocks_only_under_blocking(self):
        queue = self.repo / gt_pending.QUEUE
        queue.parent.mkdir()
        queue.write_text("<<<<<<< HEAD\npending: []\n=======\npending: [{key: x}]\n>>>>>>> other\n")
        self.warn("src/a.py")
        proc = self.hook("ground-truth-sync-check.sh")
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertRegex(proc.stderr, r"\Aground-truth: pending.yaml unreadable: .+; "
                                      r"resolve conflict or delete the file\n\Z")
        status = self.repo / "STATUS.yaml"
        status.write_text(status.read_text().replace("blocking", "advisory"))
        proc = self.hook("ground-truth-sync-check.sh")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("ground-truth (advisory): pending.yaml unreadable: ", proc.stderr)

    def test_status_classes_parse_from_real_verify_output(self):
        repo = Path(self.temp.name) / "parse"
        tools = repo / "tools/ground_truth"
        tools.mkdir(parents=True)
        for name in ("verify.py", "contract_lib.py", "gt_store.py"):
            shutil.copyfile(SKILL / "templates" / name, tools / name)
        (repo / "app dir").mkdir()
        (repo / "app dir/core.py").write_text("def price():\n    return 1\n")
        (repo / "app dir/new file.py").write_text("x = 1\n")
        (repo / "tests").mkdir()
        (repo / "tests/test_core.py").write_text("def test_price():\n    pass\n")
        (repo / "STATUS.yaml").write_text(yaml.safe_dump({
            "schema_version": 1,
            "meta": {"repo": "parse", "enforcement": "advisory", "last_audited": "2026-10-13",
                     "coverage": {"roots": ["app dir/"], "runner": "pytest"}},
            "claims": [{"id": "price", "component": "price", "kind": "status", "status": "implemented",
                        "path": ["app dir/core.py", "app dir/it's gone.py"], "check_kind": "pytest",
                        "check": "tests/test_core.py::test_price", "note": "see tests/test_core.py:2 «assert»"}],
            "debt": [{"id": "price_debt", "ref": "price", "state": "open", "severity": "blocking",
                      "description": "left open", "opened": "2026-10-01"}]}, allow_unicode=True))
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        proc = subprocess.run([sys.executable, str(tools / "verify.py"), "--mode=sync", "--show-warn"],
                              cwd=repo, capture_output=True, text=True)
        found = {(cls, obj, area) for cls, obj, area, _ in gt_pending.status_findings(repo, proc.stdout.splitlines())}
        self.assertEqual(found, {
            ("missing-path", "price:app dir/it's gone.py", "app dir"),
            ("uncovered", "app dir/new file.py", "app dir"),
            ("cited-line", "price:tests/test_core.py:64b5daad", "tests"),
            ("no-assertions", "price:tests/test_core.py::test_price", "tests"),
            ("open-debt", "price_debt", "app dir"),
            ("no-canary", "app dir/", "app dir")}, proc.stdout)

    def test_gone_problem_removes_entry(self):
        self.warn("src/a.py")
        self.hook("ground-truth-sync-check.sh")
        self.warn()
        proc = self.hook("ground-truth-sync-check.sh")
        self.assertIn("resolved 1: uncovered:src/a.py", proc.stderr)
        self.assertEqual(self.entries(), {})

    def test_area_overflow_blocks_only_a_touched_area(self):
        self.warn(*(f"src/m{i}.py" for i in range(6)))
        self.assertEqual(self.hook("ground-truth-sync-check.sh", touch="docs/a.md").returncode, 0)
        subprocess.run(["git", "-C", str(self.repo), "checkout", "-q", "--", "docs"], check=True)
        proc = self.hook("ground-truth-sync-check.sh")
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("area src has 6/5 entries", proc.stderr)
        self.assertEqual(len(proc.stderr.strip().splitlines()), 1, proc.stderr)

    def test_escalation_by_sessions_and_by_age(self):
        self.warn("src/a.py")
        for session in ("s1", "s2"):
            self.assertEqual(self.hook("ground-truth-sync-check.sh", session).returncode, 0)
        proc = self.hook("ground-truth-sync-check.sh", "s3")
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("seen in 3 sessions", proc.stderr)
        # Sessions are local: CI never escalates on them.
        self.assertEqual(gt_pending.ci_problems(self.repo, set(self.entries()), set(gt_pending.STATUS_CLASSES)), [])
        (self.repo / ".git" / gt_pending.SESSIONS).unlink()
        self.edit_entry("uncovered:src/a.py", first_seen={"date": "2000-01-01", "rev": "-"})
        proc = self.hook("ground-truth-sync-check.sh", "s3")
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertRegex(proc.stderr, r"first seen \d+d ago \(> 7d\)")
        (self.repo / ".git" / gt_pending.SESSIONS).unlink()
        self.edit_entry("uncovered:src/a.py", first_seen={"date": gt_pending.date.today().isoformat(), "rev": "-"},
                        batches_survived=2)
        proc = self.hook("ground-truth-sync-check.sh", "s3")
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("survived 2 batch reviews (>= 2)", proc.stderr)

    def test_kept_entry_does_not_escalate_by_sessions(self):
        self.warn("src/a.py")
        self.hook("ground-truth-sync-check.sh", "s1")
        verdicts = Path(self.temp.name) / "v.yaml"
        verdicts.write_text("- key: uncovered:src/a.py\n  verdict: Keep\n  evidence: refactor lands next week\n")
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(gt_pending.main(["--root", str(self.repo), "batch", "--verdicts", str(verdicts)]), 0)
        for session in ("s2", "s3"):
            proc = self.hook("ground-truth-sync-check.sh", session)
            self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("sessions", proc.stderr)

    def test_ci_counts_only_entries_still_found(self):
        self.warn(*(f"m{i}/a.py" for i in range(31)))
        self.hook("ground-truth-sync-check.sh")
        classes = set(gt_pending.STATUS_CLASSES)
        self.assertEqual(gt_pending.ci_problems(self.repo, set(self.entries()), classes),
                         [f"pending queue has 31/30 entries; run the batch review of {gt_pending.QUEUE}"])
        self.assertEqual(gt_pending.ci_problems(self.repo, {"uncovered:m0/a.py"}, classes), [])

    def test_session_start_line_is_stable(self):
        self.assertNotIn("ground-truth pending", self.hook("ground-truth-session-start.sh", touch=None).stdout)
        self.warn("src/a.py", "docs/a.md")
        self.hook("ground-truth-sync-check.sh")
        first = self.hook("ground-truth-session-start.sh", touch=None).stdout
        # The static card comes first; the queue line carries a date, not an age.
        today = gt_pending.date.today()
        self.assertTrue(first.startswith("## ground-truth contract active"), first)
        self.assertIn(f"\nground-truth pending 2/30, oldest {today.isoformat()}, escalated 0\n", first)
        self.assertLess(first.index("Details: .claude/rules"), first.index("ground-truth pending"))
        self.assertEqual(self.hook("ground-truth-session-start.sh", touch=None).stdout, first)
        tomorrow = today.fromordinal(today.toordinal() + 1).isoformat()
        self.assertEqual(self.hook("ground-truth-session-start.sh", touch=None, today=tomorrow).stdout, first)


class AutoRepairChecks(unittest.TestCase):
    """The Stop hook repairs content-proven line shifts in STATUS.yaml before the real verify reads it."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="gt-repair-test-")
        self.repo = Path(self.temp.name)
        tools = self.repo / "tools/ground_truth"
        tools.mkdir(parents=True)
        for name in ("verify.py", "contract_lib.py", "gt_store.py", "gt_pending.py", "remap_line_refs.py"):
            shutil.copyfile(SKILL / "templates" / name, tools / name)
        (self.repo / "src").mkdir()
        self.code = self.repo / "src/core.py"
        self.code.write_text("def price():\n    return 1\n\n\ndef tax():\n    return 2\n")
        self.counter = self.repo / ".git" / gt_pending.REPAIRED
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)

    def tearDown(self):
        self.temp.cleanup()

    def status(self, *notes, commit=True, enforcement="advisory"):
        claims = "".join(f"  - id: c{i}\n    component: c{i}\n    kind: status\n    status: implemented\n"
                         f"    path: src/core.py\n    check_kind: pytest\n    check: tests/test_core.py::test_x\n"
                         f"    note: {note}\n" for i, note in enumerate(notes))
        (self.repo / "STATUS.yaml").write_text(
            f"schema_version: 1\nmeta:\n  repo: fixture\n  enforcement: {enforcement}\n  last_audited: '2026-10-13'\n"
            "  coverage:\n    roots: [src/]\n    runner: pytest\nclaims:\n" + claims)
        if commit:
            subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=fixture", "-c",
                            "user.email=fixture@example.invalid", "commit", "-qm", "fixture"], check=True)

    def shift(self, text="# a\n# b\n# c\n"):
        self.code.write_text(text + self.code.read_text())

    def stop(self):
        return subprocess.run(["bash", str(SKILL / "templates/ground-truth-sync-check.sh")],
                              input=json.dumps({"cwd": str(self.repo), "session_id": "s1"}),
                              text=True, capture_output=True, env={**os.environ, "GT_PYTHON": sys.executable})

    def notes(self):
        return [c["note"] for c in yaml.safe_load((self.repo / "STATUS.yaml").read_text())["claims"]]

    def cited(self):
        return {k: e for k, e in gt_pending.load(self.repo)[2].items() if e["class"] == "cited-line"}

    def test_unique_quote_and_range_follow_their_lines_once(self):
        self.status("see src/core.py:5 «def tax():»", "see src/core.py:5-6 «return 2»")
        self.shift()
        proc = self.stop()
        self.assertIn("ground-truth: auto-repaired 2 line refs in STATUS.yaml\n", proc.stderr)
        self.assertEqual(self.notes(), ["see src/core.py:8 «def tax():»", "see src/core.py:8-9 «return 2»"])
        self.assertEqual(self.cited(), {})
        self.assertEqual(self.counter.read_text().strip(), "2")
        before = (self.repo / "STATUS.yaml").read_bytes()
        proc = self.stop()
        self.assertNotIn("auto-repaired", proc.stderr)
        self.assertEqual((self.repo / "STATUS.yaml").read_bytes(), before)
        self.assertEqual(self.counter.read_text().strip(), "2")

    def test_quote_found_twice_or_nowhere_is_left_for_the_queue(self):
        self.status("see src/core.py:2 «return»", "see src/core.py:5 «def gone»")
        self.shift()
        proc = self.stop()
        self.assertNotIn("auto-repaired", proc.stderr)
        self.assertEqual(self.notes(), ["see src/core.py:2 «return»", "see src/core.py:5 «def gone»"])
        self.assertFalse(self.counter.exists())
        sha = lambda quote: hashlib.sha1(quote.encode()).hexdigest()[:8]
        self.assertEqual(sorted(self.cited()), [f"cited-line:c0:src/core.py:{sha('return')}",
                                                f"cited-line:c1:src/core.py:{sha('def gone')}"])

    def test_cited_line_key_survives_a_new_line_number(self):
        self.status("see src/core.py:2 «return»")
        self.shift()
        self.stop()
        [key] = self.cited()
        queue = self.repo / gt_pending.QUEUE
        queue.write_text(queue.read_text().replace(gt_pending.date.today().isoformat(), "2026-10-10"))
        self.status("see src/core.py:4 «return»", commit=False)
        proc = self.stop()
        self.assertNotIn("queued", proc.stderr)
        self.assertEqual(list(self.cited()), [key])
        self.assertEqual(self.cited()[key]["first_seen"]["date"], "2026-10-10")

    def test_bare_citation_moves_only_with_unchanged_content(self):
        self.status("see src/core.py:5 for tax", "see src/core.py:2 for price")
        self.shift()
        self.code.write_text(self.code.read_text().replace("return 1", "return 10"))
        proc = self.stop()
        self.assertIn("ground-truth: auto-repaired 1 line refs in STATUS.yaml\n", proc.stderr)
        self.assertEqual(self.notes(), ["see src/core.py:8 for tax", "see src/core.py:2 for price"])
        self.assertIn("src/core.py:2 -> 5", proc.stderr)
        self.assertEqual(self.counter.read_text().strip(), "1")

    def test_removed_line_and_short_quote_do_not_move(self):
        # The comment mentions the removed function; a one-character quote matches anywhere.
        self.status("see src/core.py:5 «def tax():»", "see src/core.py:2 «1»")
        self.code.write_text("# def tax(): was removed, see billing\ndef price():\n    return 10\n")
        proc = self.stop()
        self.assertNotIn("auto-repaired", proc.stderr)
        self.assertEqual(self.notes(), ["see src/core.py:5 «def tax():»", "see src/core.py:2 «1»"])
        self.assertFalse(self.counter.exists())

    def test_repair_shift_does_not_open_the_edit_guard(self):
        shutil.copyfile(SKILL / "templates/gt_edit_guard.py", self.repo / "tools/ground_truth/gt_edit_guard.py")
        self.status("see src/core.py:5 «def tax():»", enforcement="blocking")
        self.shift("# header\n")
        self.assertIn("auto-repaired 1", self.stop().stderr)
        self.assertEqual(self.notes(), ["see src/core.py:6 «def tax():»"])
        event = {"cwd": str(self.repo), "session_id": "s1", "tool_name": "Write",
                 "tool_input": {"file_path": str(self.repo / "src/other.py"), "content": "y"}}
        proc = subprocess.run([sys.executable, "tools/ground_truth/gt_edit_guard.py"], cwd=self.repo,
                              input=json.dumps(event), text=True, capture_output=True)
        self.assertEqual(proc.returncode, 2, proc.stderr)

    def test_garbage_counter_reads_as_zero(self):
        self.counter.parent.mkdir(exist_ok=True)
        self.counter.write_text("1x\n")
        self.status("see src/core.py:5 «def tax():»")
        self.shift()
        proc = self.stop()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.counter.read_text().strip(), "1")

    def test_citation_outside_the_repository_is_not_read(self):
        outside = os.path.relpath(SKILL / "SKILL.md", self.repo)
        self.assertTrue(outside.startswith(".."))
        proc = subprocess.run([sys.executable, "-c", "import sys, remap_line_refs as r\n"
                               "print(len(r.lines_at(None, sys.argv[1])), len(r.lines_at(None, 'src/core.py')))",
                               outside], cwd=self.repo, capture_output=True, text=True,
                              env={**os.environ, "PYTHONPATH": str(self.repo / "tools/ground_truth")})
        self.assertEqual(proc.stdout, "0 6\n", proc.stderr)


class BatchReviewChecks(unittest.TestCase):
    """Batch review of the queue: verdicts, run log, report, class header, due line."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="gt-batch-test-")
        self.repo = Path(self.temp.name)
        (self.repo / "STATUS.yaml").write_text("meta:\n  enforcement: blocking\nclaims:\n- id: price_v2\n")
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        self.queue = self.repo / gt_pending.QUEUE

    def tearDown(self):
        self.temp.cleanup()

    def seed(self, *keys):
        """Queue `class:path` keys as a Stop would; the queue keeps only these STATUS-class keys."""
        gt_pending.record(self.repo, [(k.split(":")[0], k.split(":", 1)[1], "src", f"ev {k}") for k in keys],
                          set(gt_pending.STATUS_CLASSES), "s1")

    def batch(self, verdicts):
        path = self.repo / "verdicts.yaml"
        path.write_text(yaml.safe_dump(verdicts))
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            rc = gt_pending.main(["--root", str(self.repo), "batch", "--verdicts", str(path)])
        return rc, err.getvalue()

    def out(self, *argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            gt_pending.main(["--root", str(self.repo), *argv])
        return buf.getvalue()

    def log(self):
        return yaml.safe_load((self.repo / gt_pending.LOG).read_text())

    def edit(self, key=None, **fields):
        data = yaml.safe_load(self.queue.read_text())
        (next(e for e in data["pending"] if e["key"] == key) if key else data).update(fields)
        self.queue.write_text(yaml.safe_dump(data, sort_keys=False))

    def test_each_verdict(self):
        self.seed(*(f"uncovered:src/{n}.py" for n in "abcdef"))
        rc, err = self.batch([
            {"key": "uncovered:src/a.py", "verdict": "Update", "evidence": "claim price covers it"},
            {"key": "uncovered:src/b.py", "verdict": "Replace", "evidence": "now STATUS.yaml#price_v2"},
            {"key": "uncovered:src/c.py", "verdict": "Consolidate", "into": "uncovered:src/f.py", "evidence": "same file"},
            {"key": "uncovered:src/d.py", "verdict": "Delete", "noise": True, "evidence": "generated file"},
            {"key": "uncovered:src/e.py", "verdict": "Keep", "evidence": "refactor lands next week"}])
        self.assertEqual(rc, 0, err)
        entries = gt_pending.load(self.repo)[2]
        self.assertEqual(sorted(entries), ["uncovered:src/e.py", "uncovered:src/f.py"])
        self.assertEqual(entries["uncovered:src/e.py"]["kept"],
                         {"date": gt_pending.date.today().isoformat(), "evidence": "refactor lands next week"})
        self.assertEqual(entries["uncovered:src/e.py"]["batches_survived"], 0)
        self.assertEqual(entries["uncovered:src/f.py"]["batches_survived"], 1)
        [block] = self.log()
        self.assertEqual(block["verdicts"], {"Keep": 1, "Update": 1, "Consolidate": 1, "Replace": 1, "Delete": 1})
        self.assertEqual((block["keys"], block["flow"], block["closed"], block["noise"], block["carried"],
                          block["age"], block["auto_repaired"]),
                         (sorted(entries), {"in": 6, "out": 4}, {"uncovered": 4}, {"uncovered": 1}, 1,
                          {"median": 0, "max": 0}, 0))
        # A kept entry still counts against the limits.
        self.assertIn("2/30 entries", self.out("report"))

    def test_batch_logs_and_clears_the_auto_repair_count(self):
        self.seed("uncovered:src/a.py", "uncovered:src/b.py")
        counter = self.repo / ".git" / gt_pending.REPAIRED
        counter.write_text("3\n")
        self.assertEqual(self.batch([{"key": "uncovered:src/a.py", "verdict": "Update", "evidence": "claim added"}])[0], 0)
        self.assertEqual(self.log()[-1]["auto_repaired"], 3)
        self.assertFalse(counter.exists())
        self.assertIn("auto-repaired 3/4 of repaired or closed by verdict", self.out("report"))
        self.assertEqual(self.batch([])[0], 0)
        self.assertEqual(self.log()[-1]["auto_repaired"], 0)

    def test_closed_finding_seen_again_keeps_its_age(self):
        self.seed("uncovered:src/a.py")
        self.edit("uncovered:src/a.py", first_seen={"date": "2000-01-01", "rev": "-"})
        self.assertEqual(self.batch([{"key": "uncovered:src/a.py", "verdict": "Update", "evidence": "claim added"}])[0], 0)
        self.assertEqual(gt_pending.load(self.repo)[2], {})
        self.seed("uncovered:src/a.py")
        entry = gt_pending.load(self.repo)[2]["uncovered:src/a.py"]
        self.assertEqual((entry["first_seen"]["date"], entry["batches_survived"], entry["escalated"]),
                         ("2000-01-01", 1, True))
        data = yaml.safe_load(self.queue.read_text())
        self.assertEqual((data.get("closed"), data["reopened"]), (None, 1))
        self.assertEqual(self.batch([])[0], 0)
        self.assertEqual(self.log()[-1]["reopened"], 1)
        self.assertIn("reopened after a verdict 1", self.out("report"))
        self.assertNotIn("reopened", yaml.safe_load(self.queue.read_text()))

    def test_tombstone_goes_when_the_finding_is_gone(self):
        self.seed("uncovered:src/a.py", "uncovered:src/b.py")
        self.batch([{"key": "uncovered:src/a.py", "verdict": "Delete", "evidence": "file removed"}])
        self.assertEqual(list(yaml.safe_load(self.queue.read_text())["closed"]), ["uncovered:src/a.py"])
        self.seed("uncovered:src/b.py")
        self.assertNotIn("closed", yaml.safe_load(self.queue.read_text()))

    def test_ci_skips_an_ignored_class(self):
        self.seed("uncovered:src/a.py")
        self.edit("uncovered:src/a.py", first_seen={"date": "2000-01-01", "rev": "-"})
        classes = set(gt_pending.STATUS_CLASSES)
        self.assertEqual(len(gt_pending.ci_problems(self.repo, {"uncovered:src/a.py"}, classes)), 1)
        self.edit(classes={"uncovered": "ignore"})
        self.assertEqual(gt_pending.ci_problems(self.repo, {"uncovered:src/a.py"}, classes), [])

    def test_record_waits_for_the_lock_and_takes_over_a_stale_one(self):
        self.seed("uncovered:src/a.py")
        lock = self.repo / ".git" / gt_pending.LOCK
        lock.write_text("")
        run = lambda s: gt_pending.record(self.repo, [("uncovered", "src/a.py", "src", "ev")],
                                          set(gt_pending.STATUS_CLASSES), s)
        second = threading.Thread(target=run, args=("s2",))
        second.start()
        time.sleep(0.3)
        self.assertTrue(second.is_alive())
        lock.unlink()
        second.join(5)
        sessions = json.loads((self.repo / ".git" / gt_pending.SESSIONS).read_text())
        self.assertEqual(sessions["uncovered:src/a.py"], ["s1", "s2"])
        lock.write_text("")
        os.utime(lock, (time.time() - 60, time.time() - 60))
        run("s3")
        self.assertFalse(lock.exists())

    def test_bad_verdict_file_leaves_queue_and_log(self):
        self.seed("uncovered:src/a.py", "uncovered:src/b.py")
        before = self.queue.read_bytes()
        good = {"key": "uncovered:src/b.py", "verdict": "Update", "evidence": "fixed"}
        for bad, message in (({"key": "uncovered:src/a.py", "verdict": "Keep"}, "evidence is empty"),
                             ({"key": "uncovered:src/a.py", "verdict": "Keep", "evidence": " TODO "}, "placeholder"),
                             ({"key": "uncovered:src/zz.py", "verdict": "Delete", "evidence": "x y"}, "not in the queue"),
                             (good, "given twice"),
                             ({"key": "uncovered:src/a.py", "verdict": "Fix", "evidence": "x y"}, "verdict must be one of"),
                             ({"key": "uncovered:src/a.py", "verdict": "Consolidate", "evidence": "dup"}, "needs into"),
                             ({"key": "uncovered:src/a.py", "verdict": "Replace", "evidence": "STATUS.yaml#gone"},
                              "existing claim"),
                             ({"key": "uncovered:src/a.py", "verdict": "Keep", "noise": True, "evidence": "x y"},
                              "only for Delete")):
            rc, err = self.batch([good, bad])
            self.assertEqual(rc, 1, bad)
            self.assertIn(message, err)
            self.assertEqual(self.queue.read_bytes(), before)
            self.assertFalse((self.repo / gt_pending.LOG).exists())

    def test_replace_accepts_claim_ref_in_both_layouts(self):
        def replace(evidence):
            self.seed("uncovered:src/a.py")
            return self.batch([{"key": "uncovered:src/a.py", "verdict": "Replace", "evidence": evidence}])
        self.assertEqual(replace("now claim:price_v2")[0], 0)
        (self.repo / "STATUS.yaml").unlink()
        shards = self.repo / ".ground-truth"
        (shards / "areas").mkdir(parents=True)
        (shards / "contract.yaml").write_text("schema_version: 2\nmeta:\n  enforcement: blocking\nedges: []\n")
        (shards / "areas/global.yaml").write_text("area: global\nclaims:\n- id: price_v2\n- id: price-v3\n")
        self.assertEqual(replace("now claim:price_v2")[0], 0)
        self.assertEqual(replace("now claim:price-v3.")[0], 0)
        rc, err = replace("STATUS.yaml#gone")
        self.assertEqual(rc, 1)
        self.assertIn("existing claim", err)

    def test_entries_without_verdict_escalate_after_two_batches(self):
        self.seed("uncovered:src/a.py")
        classes = set(gt_pending.STATUS_CLASSES)
        self.assertEqual(self.batch([])[0], 0)
        self.assertEqual(gt_pending.ci_problems(self.repo, {"uncovered:src/a.py"}, classes), [])
        self.assertEqual(self.batch([])[0], 0)
        self.assertEqual(gt_pending.ci_problems(self.repo, {"uncovered:src/a.py"}, classes),
                         ["pending uncovered:src/a.py escalated: survived 2 batch reviews (>= 2)"])

    def test_kept_entry_escalates_from_kept_date(self):
        self.seed("uncovered:src/a.py")
        self.edit("uncovered:src/a.py", first_seen={"date": "2000-01-01", "rev": "-"})
        self.assertTrue(gt_pending.load(self.repo)[2]["uncovered:src/a.py"]["escalated"])
        self.assertEqual(self.batch([{"key": "uncovered:src/a.py", "verdict": "Keep", "evidence": "owner accepts"}])[0], 0)
        self.assertFalse(gt_pending.load(self.repo)[2]["uncovered:src/a.py"]["escalated"])
        self.edit("uncovered:src/a.py", kept={"date": "2000-01-02", "evidence": "owner accepts"})
        self.assertRegex(gt_pending.load(self.repo)[2]["uncovered:src/a.py"]["reason"], r"^kept \d+d ago \(> 7d\)$")

    def test_flow_between_runs_and_alarm(self):
        self.seed("uncovered:src/a.py", "uncovered:src/b.py")
        self.batch([])
        self.assertEqual(self.log()[-1]["flow"], {"in": 2, "out": 0})
        self.assertIn("Alarms:\n  none\n", self.out("report"))
        self.seed("uncovered:src/a.py", "uncovered:src/b.py", "uncovered:src/c.py", "uncovered:src/d.py")
        self.batch([{"key": "uncovered:src/a.py", "verdict": "Delete", "evidence": "file removed"}])
        self.assertEqual(self.log()[-1]["flow"], {"in": 2, "out": 1})
        self.assertIn("outflow below inflow in the last 2 batch reviews", self.out("report"))
        self.seed("uncovered:src/c.py", "uncovered:src/d.py")  # b fixed between runs
        self.batch([{"key": "uncovered:src/c.py", "verdict": "Update", "evidence": "claim added"}])
        self.assertEqual(self.log()[-1]["flow"], {"in": 0, "out": 2})
        self.assertEqual(len(self.log()), 3)
        self.assertIn("Alarms:\n  none\n", self.out("report"))

    def test_report_recommends_demote_and_promote(self):
        self.seed("uncovered:src/a.py", "uncovered:src/b.py", "uncovered:src/c.py", "missing-path:src/x.py")
        self.edit("missing-path:src/x.py", first_seen={"date": "2000-01-01", "rev": "-"})
        self.batch([{"key": f"uncovered:src/{n}.py", "verdict": "Delete", "noise": n != "c", "evidence": "check misfires"}
                    for n in "abc"])
        md = self.out("report", "--format", "md")
        self.assertIn("## Class recommendations\n\n", md)
        self.assertIn("- demote uncovered: 2/3 closed as noise in the last 1 reviews (classes: {uncovered: ignore})", md)
        self.assertIn("- promote missing-path: 1 escalations in the last 1 reviews (classes: {missing-path: block})", md)
        self.assertIn("Delete as noise 2/3", md)

    def test_ignore_class_is_not_queued_and_header_survives(self):
        self.seed("uncovered:src/a.py")
        self.edit(classes={"uncovered": "ignore"})
        self.seed("uncovered:src/a.py", "missing-path:src/x.py")
        self.assertEqual(sorted(gt_pending.load(self.repo)[2]), ["missing-path:src/x.py"])
        self.assertEqual(yaml.safe_load(self.queue.read_text())["classes"], {"uncovered": "ignore"})

    def test_block_class_blocks_only_under_blocking(self):
        self.seed("uncovered:src/a.py")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(gt_pending.gate(self.repo, {"src"}), 0)
        self.edit(classes={"uncovered": "block"})
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(gt_pending.gate(self.repo, {"src"}), 1)
            self.assertEqual(gt_pending.gate(self.repo, {"docs"}), 0)
        self.assertIn("uncovered:src/a.py: class uncovered blocks", err.getvalue())
        (self.repo / "STATUS.yaml").write_text("meta:\n  enforcement: advisory\nclaims: []\n")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(gt_pending.gate(self.repo, {"src"}), 0)
        self.assertIn("ground-truth (advisory): pending queue blocks this area", err.getvalue())

    def test_batch_due_on_fill_log_age_and_escalation(self):
        self.seed("uncovered:src/a.py")
        self.assertEqual(self.out("summary"),
                         f"ground-truth pending 1/30, oldest {gt_pending.date.today()}, escalated 0\n")
        self.edit(limits={**gt_pending.LIMITS, "total": 1})
        self.assertIn("; batch review due: queue at 1/1\n", self.out("summary"))
        self.edit(limits=gt_pending.LIMITS)
        self.batch([])
        self.assertNotIn("due", self.out("summary"))
        log = self.repo / gt_pending.LOG
        log.write_text(log.read_text().replace(gt_pending.date.today().isoformat(), "2000-01-01"))
        self.assertIn("; batch review due: no batch review since 2000-01-01\n", self.out("summary"))
        log.unlink()
        self.edit("uncovered:src/a.py", first_seen={"date": "2000-01-01", "rev": "-"})
        self.assertIn("; batch review due: no batch review since 2000-01-01, 1 escalated\n", self.out("summary"))

    def ttl(self, claims, debt=(), edges=(), today="2026-04-02"):
        """A contract nobody read since the local record began on 2026-01-01; one batch review on `today`."""
        (self.repo / "STATUS.yaml").write_text(yaml.safe_dump(
            {"meta": {"enforcement": "blocking"}, "claims": claims, "debt": list(debt), "edges": list(edges)}))
        (self.repo / ".git/ground-truth-usage.json").write_text('{"since": "2026-01-01", "last": {}}')
        return self.batch_on(today, [])

    def batch_on(self, today, verdicts):
        with mock.patch.dict(os.environ, {"GT_TODAY": today}):
            return self.batch(verdicts)

    def unused(self):
        return {k: e for k, e in gt_pending.load(self.repo)[2].items() if e["class"] == "unused"}

    def test_unused_claim_after_ttl_and_each_exemption(self):
        (self.repo / "docs").mkdir()
        (self.repo / "docs/notes.md").write_text("see claim:doc and STATUS.yaml#doc2\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "docs"], check=True)
        claims = [{"id": "plain", "path": "src/plain.py"}, {"id": "canary", "canary": True},
                  {"id": "mut", "mutation": {"file": "src/plain.py"}}, {"id": "live", "kind": "live_state"},
                  {"id": "owed"}, {"id": "doc"}, {"id": "doc2"}, {"id": "edge"}]
        debt = [{"id": "D1", "ref": "owed", "state": "open"}]
        edges = [{"from": "edge", "to": "other-repo:api", "probe": "probes/api.sh"}]
        self.assertEqual(self.ttl(claims, debt, edges, today="2026-03-31")[0], 0)  # the record is 89 days old
        self.assertEqual(self.unused(), {})
        self.assertEqual(self.batch_on("2026-04-02", [])[0], 0)
        self.assertEqual({k: (e["area"], e["paths"], e["evidence"]) for k, e in self.unused().items()},
                         {"unused:plain": ("src", ["src/plain.py"], "no recorded use after 2026-01")})
        (self.repo / ".git/ground-truth-usage.json").write_text('{"since": "2026-01-01", "last": {"plain": "2026-01-03"}}')
        self.queue.unlink()
        self.assertEqual(self.batch_on("2026-04-02", [])[0], 0)  # read 89 days ago
        self.assertEqual(self.unused(), {})

    def test_at_most_five_unused_per_batch(self):
        self.ttl([{"id": f"u{i}"} for i in range(7)])
        self.assertEqual(sorted(self.unused()), [f"unused:u{i}" for i in range(5)])
        self.assertEqual(self.batch_on("2026-04-02", [])[0], 0)
        self.assertEqual(len(self.unused()), 7)

    def test_unused_verdicts(self):
        claims = [{"id": "plain", "path": "src/plain.py"}, {"id": "wide", "path": "src"}, {"id": "narrow", "path": "src/n.py"}]
        self.ttl(claims)
        self.assertEqual(sorted(self.unused()), ["unused:narrow", "unused:plain", "unused:wide"])
        before = self.queue.read_bytes()
        bad = [({"key": "unused:plain", "verdict": "Update", "evidence": "claim fixed"}, "takes Keep, Delete or"),
               ({"key": "unused:plain", "verdict": "Replace", "evidence": "claim:wide"}, "takes Keep, Delete or"),
               ({"key": "unused:plain", "verdict": "Delete", "evidence": "dead"}, "removed from the contract")]
        for item, message in bad:
            rc, err = self.batch_on("2026-04-02", [item])
            self.assertEqual(rc, 1, item)
            self.assertIn(message, err)
        (self.repo / "STATUS.yaml").write_text(yaml.safe_dump({"meta": {}, "claims": claims[1:],
                                                               "debt": [{"id": "D1", "ref": "plain", "state": "open"}]}))
        rc, err = self.batch_on("2026-04-02", [{"key": "unused:plain", "verdict": "Delete", "evidence": "dead"}])
        self.assertEqual(rc, 1)
        self.assertIn("no open debt on it", err)
        (self.repo / "STATUS.yaml").write_text(yaml.safe_dump({"meta": {}, "claims": claims[1:]}))
        for into, message in (("claim:narrow", "claim:narrow paths do not cover src/plain.py"),
                              ("claim:gone", "needs into: claim:<id> of an existing claim"),
                              ("unused:wide", "needs into: claim:<id> of an existing claim")):
            rc, err = self.batch_on("2026-04-02", [{"key": "unused:plain", "verdict": "Consolidate", "into": into,
                                                    "evidence": "same file"}])
            self.assertEqual(rc, 1, into)
            self.assertIn(message, err)
        self.assertEqual(self.queue.read_bytes(), before)
        rc, err = self.batch_on("2026-04-20", [
            {"key": "unused:plain", "verdict": "Consolidate", "into": "claim:wide", "evidence": "wide covers it"},
            {"key": "unused:narrow", "verdict": "Keep", "evidence": "read by the billing runbook"}])
        self.assertEqual(rc, 0, err)
        self.assertEqual(sorted(self.unused()), ["unused:wide"])
        self.assertEqual(self.log()[-1]["removed"], ["plain"])
        usage = (self.repo / ".ground-truth/usage.yaml").read_bytes()
        self.assertEqual(usage, b"since: 2026-01\nclaims:\n  narrow: 2026-04\n")
        self.assertEqual(self.batch_on("2026-04-20", [])[0], 0)
        self.assertEqual((self.repo / ".ground-truth/usage.yaml").read_bytes(), usage)
        self.assertNotIn("removed", self.log()[-1])
        (self.repo / "STATUS.yaml").write_text(yaml.safe_dump({"meta": {}, "claims": claims[2:]}))
        self.assertEqual(self.batch_on("2026-04-20", [{"key": "unused:wide", "verdict": "Delete", "evidence": "dead"}])[0], 0)
        self.assertEqual((self.log()[-1]["removed"], self.unused()), (["wide"], {}))



class PacketChecks(unittest.TestCase):
    """gt context on a sharded contract with 200 claims in one area and a model.yaml receipt."""

    def setUp(self):
        import gt_packet
        self.packet = gt_packet
        self.temp = tempfile.TemporaryDirectory(prefix="gt-packet-test-")
        self.repo = repo = Path(self.temp.name)
        for i in range(200):
            (repo / f"src/big/f{i:03}.py").parent.mkdir(parents=True, exist_ok=True)
            (repo / f"src/big/f{i:03}.py").write_text(f"VALUE = {i}\n")
        (repo / "src/small").mkdir()
        (repo / "src/small/a.py").write_text("A = 1\n")
        (repo / "lib").mkdir()
        (repo / "lib/free.py").write_text("FREE = 1\n")
        (repo / "docs").mkdir()
        (repo / "docs/big.md").write_text("big area\n")
        (repo / "docs/small.md").write_text("small area\n")
        store = repo / ".ground-truth/areas"
        store.mkdir(parents=True)
        (repo / ".ground-truth/contract.yaml").write_text("schema_version: 1\nmeta:\n  enforcement: advisory\nedges: []\n")
        (repo / ".ground-truth/debt.yaml").write_text(
            "debt:\n  - id: d1\n    ref: c003\n    description: x\n    severity: normal\n    state: open\n    owner: ''\n"
            "    opened: '2026-01-01'\n")
        claims = [{"id": f"c{i:03}", "kind": "status", "status": "implemented" if i % 2 else "partial",
                   "path": f"src/big/f{i:03}.py", "check": f"tests/test_big.py::test_{i}"} for i in range(200)]
        claims[150]["canary"] = True
        claims += [{"id": "gone", "kind": "status", "status": "implemented", "canary": True, "path": "src/big/gone.py"},
                   {"id": "quoted", "kind": "status", "status": "implemented", "canary": True, "path": "src/big/f001.py",
                    "note": "reads src/big/f001.py:1 «VALUE = 999»"}]
        (store / "big.yaml").write_text(yaml.safe_dump({"area": "big", "sources": ["src/big"], "claims": claims},
                                                       sort_keys=False))
        (store / "small.yaml").write_text(yaml.safe_dump({"area": "small", "sources": ["src/small"], "claims": [
            {"id": "small_a", "kind": "status", "status": "implemented", "path": "src/small/a.py"}]}, sort_keys=False))
        (store / "free.yaml").write_text(yaml.safe_dump({"area": "free", "sources": ["lib"], "claims": [
            {"id": "free_a", "kind": "status", "status": "implemented", "path": "lib/free.py"}]}, sort_keys=False))
        model = {"version": 1, "repositories": {"app": {"path": ".", "watch": ["src", "docs"]}},
                 "areas": [{"id": area, "repo": "app", "summary": f"{area} area " + "x" * 200,
                            "sources": [f"src/{area}"], "docs": [f"docs/{area}.md"],
                            "depends_on": ["small"] if area == "big" else []} for area in ("big", "small")]}
        (repo / memory.MODEL).write_text(yaml.safe_dump(model, sort_keys=False))
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(repo), "-c", "user.name=fixture", "-c",
                        "user.email=fixture@example.invalid", "commit", "-qm", "fixture"], check=True)
        view = memory.inspect(repo)
        memory.refresh(repo, list(view["nodes"]), view["revision"], "fixture review")

    def tearDown(self):
        self.temp.cleanup()

    def context(self, *args):
        with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
            rc = self.packet.main(["--root", str(self.repo), *args])
        self.assertEqual(rc, 0, err.getvalue())
        return out.getvalue()

    def test_packet_is_capped_and_says_what_it_cut(self):
        text = self.context("--area", "big")
        lines = text.splitlines()
        pointers = [l for l in lines if l.startswith("  ") and not l.startswith(("  summary:", "  docs:", "  consumers:"))]
        self.assertLessEqual(len(lines), 150, text)
        self.assertEqual(len(pointers), 15, text)
        self.assertIn("+187 more: gt context --area big --all\n", text)
        self.assertIn("area big [receipt fresh] claims 202 shown 15 | debt open 1 | pending 0\n", text)
        summary = next(l for l in lines if l.startswith("  summary: "))
        self.assertLessEqual(len(summary) - len("  summary: "), 120)
        self.assertIn("  docs: docs/big.md\n", text)
        self.assertNotIn("big area\n", text)  # a doc body is never printed
        everything = self.context("--area", "big", "--all")
        self.assertNotIn("more:", everything)
        self.assertEqual(len(everything.splitlines()), 202 + 5)

    def test_drifted_claims_are_never_facts(self):
        packet = json.loads(self.context("--area", "big", "--json"))
        facts = [f["id"] for a in packet["areas"] for f in a["facts"]]
        self.assertEqual({d["id"]: d["reason"] for d in packet["drifted"]},
                         {"gone": "src/big/gone.py missing",
                          "quoted": "cited line src/big/f001.py:1 does not contain «VALUE = 999»"})
        self.assertEqual(facts[0], "c150")  # canary and nothing on the path ranks first
        self.assertNotIn("gone", facts)
        self.assertNotIn("quoted", facts)
        packet = json.loads(self.context("--path", "src/big/f004.py", "--json"))
        self.assertEqual(packet["areas"][0]["facts"][0]["id"], "c004")  # exact path first
        (self.repo / "src/small/a.py").write_text("A = 2\n")
        packet = json.loads(self.context("--path", "src/big/f004.py", "src/small/a.py", "--json"))
        self.assertIn({"id": "small_a", "area": "small", "reason": "area receipt stale"}, packet["drifted"])
        self.assertIn({"id": "c004", "area": "big", "reason": "area receipt stale"}, packet["drifted"])  # big depends on small
        self.assertEqual([f for a in packet["areas"] for f in a["facts"]], [])

    def test_same_checkout_same_bytes_and_json_has_the_same_content(self):
        first = self.context("--path", "src/big/f010.py")
        self.assertEqual(self.context("--path", "src/big/f010.py"), first)
        packet = json.loads(self.context("--path", "src/big/f010.py", "--json"))
        self.assertEqual(self.packet.render(packet), first)
        self.assertEqual(sorted(packet), ["areas", "drifted", "layout", "more", "query", "rev"])

    def test_changed_since_last_green_verify_is_marked_not_drifted(self):
        # lib/ is outside the model's watch: no receipt, so an edit does not make free_a drifted.
        self.packet.write_digests(self.repo)
        self.assertNotIn("changed since", self.context("--path", "lib/free.py"))
        (self.repo / "lib/free.py").write_text("FREE = 2\n")
        text = self.context("--path", "lib/free.py")
        self.assertIn("area free [no receipt] claims 1 shown 1 | debt open 0 | pending 0\n"
                      "  free_a implemented lib/free.py (changed since last green verify)\n", text)
        self.assertNotIn("drifted", text)

    def test_show_prints_one_entry_whole(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(self.packet.show_main(["--root", str(self.repo), "quoted"]), 0)
        self.assertIn("note: \"reads src/big/f001.py:1 \\xABVALUE = 999\\xBB\"", out.getvalue())
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(self.packet.show_main(["--root", str(self.repo), "area:small"]), 0)
        self.assertEqual(yaml.safe_load(out.getvalue())["claims"], ["small_a"])
        with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.packet.show_main(["--root", str(self.repo), "src/nothing.py"]), 1)

    def usage(self):
        path = self.repo / ".git" / "ground-truth-usage.json"
        return json.loads(path.read_text()) if path.is_file() else None

    def test_usage_touch_by_context_show_and_session_not_verify(self):
        import gt_usage
        import verify
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            verify.main(["--mode=sync", "--root", str(self.repo)])
        self.assertIsNone(self.usage())
        with mock.patch.dict(os.environ, {"GT_TODAY": "2026-03-01"}):
            packet = json.loads(self.context("--area", "big", "--json"))
        facts = [f["id"] for a in packet["areas"] for f in a["facts"]]
        self.assertEqual(len(facts), 13)  # 15 shown, 2 of them drifted; "+187 more" is not read
        self.assertEqual(self.usage(), {"since": "2026-03-01", "last": dict.fromkeys(facts, "2026-03-01")})
        with mock.patch.dict(os.environ, {"GT_TODAY": "2026-03-02"}), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.packet.show_main(["--root", str(self.repo), "gone"]), 0)
        self.assertEqual(self.usage()["last"]["gone"], "2026-03-02")
        (self.repo / "src/small/a.py").write_text("A = 3\n")
        with mock.patch.dict(os.environ, {"GT_TODAY": "2026-03-03"}):
            gt_usage.main(["--root", str(self.repo), "touch-session"])
        usage = self.usage()
        self.assertEqual((usage["since"], usage["last"]["small_a"], len(usage["last"])), ("2026-03-01", "2026-03-03", 15))


if __name__ == "__main__":
    unittest.main()
