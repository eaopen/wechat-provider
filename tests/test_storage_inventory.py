import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from wechat_provider.service import Service
from wechat_provider.storage_inventory import StorageInventory, capabilities
from wechat_provider.store import Store


@unittest.skipUnless(os.name == "posix", "P0 metadata inventory requires POSIX")
class StorageTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / "selected"
        self.source.mkdir()
        self.manager = StorageInventory(self.root / "state", "fixture")

    def start(self, roots=None, limit=250000):
        return self.manager.start(
            roots or [{"path": str(self.source), "kind": "active_data"}], limit, launch=False
        )["job_id"]

    def test_hardlinks_across_sources_sparse_files_and_no_content_reads(self):
        file = self.source / "虚构合同.pdf"
        file.write_bytes(b"synthetic" * 1024)
        other = self.root / "backup"
        other.mkdir()
        os.link(file, other / "second.pdf")
        os.link(file, self.root / "outside-scope.pdf")
        sparse = self.source / "large.mp4"
        with sparse.open("wb") as handle:
            handle.truncate(2 * 1024 * 1024)
        before = (file.stat().st_mtime_ns, file.stat().st_size)
        job = self.start(
            [
                {"path": str(self.source), "kind": "active_data"},
                {"path": str(other), "kind": "official_backup"},
            ]
        )
        with patch.object(
            Path, "read_bytes", side_effect=AssertionError("no source content reads")
        ):
            self.manager.run(job)
        report = self.manager.report(job)
        self.assertEqual(report["status"], "completed")
        self.assertEqual(report["totals"]["file_paths"], 3)
        self.assertEqual(report["totals"]["unique_files"], 2)
        self.assertEqual(report["totals"]["hardlink_aliases"], 1)
        self.assertEqual(report["totals"]["objects_with_links_outside_scope"], 1)
        self.assertEqual(
            report["totals"]["logical_bytes"], file.stat().st_size * 2 + sparse.stat().st_size
        )
        self.assertEqual(
            report["totals"]["allocated_bytes_estimate"],
            (file.stat().st_blocks + sparse.stat().st_blocks) * 512,
        )
        self.assertIsNone(report["totals"]["reclaimable_bytes"])
        self.assertFalse(report["coverage_complete"])
        self.assertEqual(report["sources"][1]["recovery_status"], "not_verified")
        self.assertEqual((file.stat().st_mtime_ns, file.stat().st_size), before)

    def test_symlink_escape_and_special_files_not_traversed(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "not-authorized.txt").write_text("synthetic")
        (self.source / "escape").symlink_to(outside, target_is_directory=True)
        os.mkfifo(self.source / "fifo")
        job = self.start()
        self.manager.run(job)
        report = self.manager.report(job)
        self.assertEqual(report["totals"]["file_paths"], 0)
        self.assertEqual(report["skipped_links_and_special"], 2)
        self.assertEqual(report["largest_files"], [])

    def test_bounds_cancel_resume_and_replay_preserve_counts(self):
        for i in range(5):
            (self.source / f"{i}.txt").write_text("synthetic")
        job = self.start(limit=2)
        self.manager.run(job)
        report = self.manager.report(job)
        self.assertEqual((report["status"], report["processed_entries"]), ("limited", 2))
        self.manager = StorageInventory(self.root / "state", "fixture")
        self.manager.resume(job, 20, launch=False)
        self.manager.cancel(job)
        self.manager.run(job)
        self.assertEqual(self.manager.report(job)["status"], "cancelled")
        self.manager.resume(job, 20, launch=False)
        self.manager.run(job)
        report = self.manager.report(job)
        self.assertEqual(report["status"], "completed")
        self.assertEqual(report["totals"]["file_paths"], 5)
        self.assertEqual(report["totals"]["unique_files"], 5)

    def test_permission_failure_is_unknown_not_empty_and_never_echoes_error_text(self):
        job = self.start()
        with patch.object(
            StorageInventory, "_open_dir", side_effect=PermissionError(13, "PRIVATE_TEXT")
        ):
            self.manager.run(job)
        report = self.manager.report(job)
        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["sources"][0]["access"], "access_denied")
        self.assertIsNone(report["sources"][0]["logical_bytes"])
        self.assertIsNone(report["totals"]["allocated_bytes_estimate"])
        self.assertNotIn("PRIVATE_TEXT", json.dumps(report))
        self.assertFalse(report["coverage_complete"])

    def test_directory_replacement_is_not_followed(self):
        job = self.start()
        self.source.rename(self.root / "old")
        self.source.mkdir()
        (self.source / "unexpected.txt").write_text("synthetic")
        self.manager.run(job)
        report = self.manager.report(job)
        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["totals"]["file_paths"], 0)

    def test_directory_change_keeps_observed_measurements_and_marks_partial(self):
        (self.source / "observed.txt").write_text("synthetic")
        job = self.start()
        self.manager.run(job)
        # A changed-directory observation is a consistency warning, not a loss
        # of permission: retain the measurements and explicitly mark the scope.
        with self.manager._connect(job) as db:
            self.manager._issue(db, 1, "", "directory_changed_during_scan")
            db.execute("UPDATE job SET status='partial'")
        report = self.manager.report(job)
        self.assertEqual(report["sources"][0]["access"], "partial")
        self.assertEqual(report["totals"]["logical_bytes"], len("synthetic"))
        self.assertEqual(report["totals"]["measurement_status"], "partial_or_pending")

    def test_overlapping_state_and_source_scope_are_rejected(self):
        nested = self.source / "nested"
        nested.mkdir()
        for sources in (
            [{"path": str(self.source), "kind": "other"}, {"path": str(nested), "kind": "other"}],
            [{"path": str(self.root), "kind": "other"}],
            [{"path": "relative", "kind": "other"}],
            [{"path": str(self.source), "kind": "delete"}],
        ):
            with self.assertRaises(ValueError):
                self.start(sources)
        self.assertFalse(self.manager.directory.exists())

    def test_account_isolation_and_no_global_read_only_capability(self):
        job = self.start()
        other = StorageInventory(self.root / "state", "other-account")
        with self.assertRaises(ValueError):
            other.report(job)
        self.assertFalse(capabilities()["application_policy"]["source_mutations"])
        self.assertNotIn("read_only", capabilities())
        adapter = Mock()
        service = Service(Store(self.root / "messages.db"), adapter)
        with self.assertRaises(ValueError):
            service.storage_inventory("delete", job_id=job)
        self.assertEqual(adapter.mock_calls, [])

    def test_state_parent_alias_resolves_but_state_root_symlink_is_rejected(self):
        parent = self.root / "actual-state-parent"
        parent.mkdir()
        alias = self.root / "alias"
        alias.symlink_to(parent, target_is_directory=True)
        manager = StorageInventory(alias / "inventory", "fixture")
        job = manager.start([{"path": str(self.source), "kind": "other"}], launch=False)
        manager.run(job["job_id"])
        self.assertEqual(manager.report(job["job_id"])["status"], "completed")
        state_alias = self.root / "state-alias"
        state_alias.symlink_to(manager.directory, target_is_directory=True)
        with self.assertRaises(ValueError):
            StorageInventory(state_alias)

    def test_real_cli_detached_worker_finishes_and_keeps_private_state(self):
        (self.source / "fixture.txt").write_text("synthetic")
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "wechat_provider.cli",
                "storage",
                "--state-dir",
                str(self.root / "cli-state"),
                "scan",
                "--source",
                "official_backup=" + str(self.source),
                "--wait",
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
        report = json.loads(result.stdout)
        self.assertEqual(report["status"], "completed")
        self.assertEqual(report["totals"]["file_paths"], 1)
        path = self.root / "cli-state" / report["job_id"] / "inventory.db"
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
