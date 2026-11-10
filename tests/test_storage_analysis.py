import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from wechat_provider.storage_analysis import analyze_inventory
from wechat_provider.storage_inventory import StorageInventory


class BackupAnalysisTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.manager = StorageInventory(self.home / "state")
        self.active, self.backup, self.old = [self.home / n for n in ("active", "backup", "old")]
        for path in (self.active, self.backup, self.old):
            path.mkdir()

    def inventory(self):
        job = self.manager.start(
            [
                {"path": str(self.active), "kind": "active_data"},
                {"path": str(self.backup), "kind": "official_backup"},
                {"path": str(self.old), "kind": "official_backup"},
            ],
            launch=False,
        )
        self.manager.run(job["job_id"])
        return job["job_id"]

    def test_saved_metadata_proves_only_sharing_not_content_or_recovery(self):
        (self.active / "file").write_bytes(b"fixture")
        os.link(self.active / "file", self.backup / "alias")
        os.link(self.active / "file", self.backup / "second-alias")
        # A same-content copy with a different inode is not shared identity.
        (self.old / "copy").write_bytes(b"fixture")
        (self.old / "Backup.db").write_bytes(b"synthetic opaque index")
        job = self.inventory()
        # No second source traversal or content read is necessary, even after
        # the selected roots have become unavailable.
        for path in (self.active, self.backup, self.old):
            path.rename(path.with_name(path.name + "-moved"))
        with (
            patch("os.scandir", side_effect=AssertionError("no source rescan")),
            patch.object(Path, "open", side_effect=AssertionError("no source content reads")),
        ):
            report = analyze_inventory(self.manager, job)
        self.assertFalse(report["scan_started"])
        self.assertFalse(report["source_contents_read"])
        self.assertFalse(report["source_mutations"])
        self.assertEqual(report["content_overlap"], "not_checked")
        self.assertIsNone(report["reclaimable_bytes"])
        self.assertEqual(report["recovery_status"], "not_verified")
        self.assertEqual(
            report["shared_sources"],
            [
                {
                    "source_a": 1,
                    "source_b": 2,
                    "shared_objects": 1,
                    "shared_logical_bytes": 7,
                    "b_unshared_objects": 0,
                    "b_unshared_logical_bytes": 0,
                }
            ],
        )
        by_id = {p["source_id"]: p for p in report["backups"]}
        self.assertEqual(by_id[2]["hardlink_aliases"], 1)
        self.assertEqual(by_id[3]["backup_index_files"], 1)
        self.assertEqual(by_id[3]["next_check"], "compatible_index_reader")
        self.assertEqual(by_id[3]["content_coverage"], "not_checked")
        self.assertEqual(by_id[3]["account_match"], "not_verified")
        self.assertEqual(by_id[3]["recommendation"], "retain_pending_verification")
        self.assertEqual(report["summary"]["unique_files"], 3)
        self.assertEqual(report["summary"]["unique_logical_bytes"], 36)

    def test_changed_identities_are_not_affirmative_sharing(self):
        (self.active / "file").write_text("fixture")
        os.link(self.active / "file", self.backup / "alias")
        job = self.inventory()
        with self.manager._connect(job) as db:
            db.execute("UPDATE entries SET size=size+1 WHERE source=2")
        self.assertEqual(analyze_inventory(self.manager, job)["shared_sources"], [])

    def test_partial_and_inaccessible_results_remain_explicit(self):
        job = self.inventory()
        with self.manager._connect(job) as db:
            db.execute("UPDATE job SET status='cancelled'")
            db.execute("UPDATE sources SET access='not_checked' WHERE kind='official_backup'")
        report = analyze_inventory(self.manager, job)
        self.assertEqual(report["summary"]["measurement_status"], "partial_inventory")
        self.assertEqual(report["summary"]["unavailable_sources"], 2)
        self.assertIsNone(report["summary"]["logical_bytes"])
        self.assertIsNone(report["summary"]["allocated_bytes_estimate"])
        self.assertTrue(all(p["logical_bytes"] is None for p in report["backups"]))

    def test_running_jobs_and_other_accounts_are_rejected(self):
        job = self.inventory()
        with self.manager._connect(job) as db:
            db.execute("UPDATE job SET status='running'")
        with self.assertRaises(ValueError):
            analyze_inventory(self.manager, job)
        with self.assertRaises(ValueError):
            analyze_inventory(StorageInventory(self.manager.directory, "other"), job)
        with self.assertRaises(ValueError):
            analyze_inventory(self.manager, "../../private")

    def test_no_backups_is_not_a_discovery_or_restore_claim(self):
        job = self.inventory()
        with self.manager._connect(job) as db:
            db.execute("UPDATE sources SET kind='other'")
        report = analyze_inventory(self.manager, job)
        self.assertEqual(report["backups"], [])
        self.assertEqual(report["summary"]["backup_sources"], 0)
        self.assertFalse(report["coverage_complete"])

    def test_cli_analyzes_existing_job_without_launching_worker(self):
        job = self.inventory()
        process = subprocess.run(
            [
                sys.executable,
                "-m",
                "wechat_provider.cli",
                "storage",
                "--state-dir",
                str(self.manager.directory),
                "analyze",
                job,
            ],
            capture_output=True,
            check=False,
            text=True,
            timeout=10,
            env={**os.environ, "WECHAT_PROVIDER_ACCOUNT": "default"},
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(json.loads(process.stdout)["job_id"], job)
