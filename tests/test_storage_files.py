import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from wechat_provider.storage_analysis import analyze_inventory
from wechat_provider.storage_files import locate_file, saved_files
from wechat_provider.storage_inventory import StorageInventory


@unittest.skipUnless(os.name == "posix", "storage and archive tests require POSIX filesystem APIs")
class StorageFilesTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.a, self.b = self.home / "a", self.home / "b"
        for p in (self.a, self.b):
            p.mkdir()
        self.manager = StorageInventory(self.home / "state")

    def scan(self):
        job = self.manager.start(
            [
                {"path": str(self.a), "kind": "official_backup"},
                {"path": str(self.b), "kind": "other"},
            ],
            launch=False,
        )
        self.manager.run(job["job_id"])
        return job["job_id"]

    def test_files_filters_pages_and_no_source_reads(self):
        for name, contents in (
            ("contract_100%.pdf", b"pdf"),
            ("clip.mp4", b"video fixture"),
            ("unshared.txt", b"document"),
        ):
            (self.a / name).write_bytes(contents)
        os.link(self.a / "contract_100%.pdf", self.b / "another-name.pdf")
        job = self.scan()
        with (
            patch("os.scandir", side_effect=AssertionError("no rescan")),
            patch.object(Path, "open", side_effect=AssertionError("no file contents")),
        ):
            page = saved_files(self.manager, job, 1, limit=1)
            self.assertEqual(page["items"][0]["path"], "clip.mp4")
            self.assertTrue(page["has_more"])
            next_page = saved_files(self.manager, job, 1, offset=1, limit=1)
            self.assertEqual(next_page["items"][0]["path"], "unshared.txt")
            result = saved_files(self.manager, job, 1, query="_100%", file_kind="document")
            self.assertEqual(result["total_file_paths"], 1)
            self.assertEqual(result["items"][0]["shared_with_other_source"], 1)
            unshared = saved_files(self.manager, job, 1, sharing="not_observed_shared")
            self.assertEqual(unshared["total_file_paths"], 2)
            self.assertEqual(saved_files(self.manager, job, query="' OR 1=1 --")["items"], [])
        for kwargs in (
            {"source_id": True},
            {"source_id": 90},
            {"offset": -1},
            {"limit": 51},
            {"query": "字" * 100},
            {"file_kind": "directory"},
            {"sharing": "deletable"},
        ):
            with self.assertRaises(ValueError):
                saved_files(self.manager, job, **kwargs)

    def test_location_checks_saved_identity_and_rejects_paths_symlinks_and_changes(self):
        folder = self.a / "nested"
        folder.mkdir()
        file = folder / "fixture.txt"
        file.write_text("synthetic")
        job = self.scan()
        with patch.object(Path, "open", side_effect=AssertionError("no content read")):
            report = locate_file(self.manager, job, 1, "nested/fixture.txt")
        self.assertTrue(report["identity_checked"])
        self.assertEqual(report["absolute_path"], str(file))
        for value in ("../outside", "/etc/passwd", "not-indexed.txt"):
            with self.assertRaises(ValueError):
                locate_file(self.manager, job, 1, value)
        file.write_text("different size")
        with self.assertRaises(ValueError):
            locate_file(self.manager, job, 1, "nested/fixture.txt")
        file.unlink()
        file.symlink_to(self.b)
        with self.assertRaises(ValueError):
            locate_file(self.manager, job, 1, "nested/fixture.txt")
        folder.rename(self.a / "renamed")
        folder.symlink_to(self.b, target_is_directory=True)
        with self.assertRaises(OSError):
            locate_file(self.manager, job, 1, "nested/fixture.txt")

    def test_backup_structures_group_aliases_and_measure_small_deltas(self):
        for dirname in ("set-one", "set-two"):
            folder = self.a / dirname
            folder.mkdir()
            for name in ("Backup.db", "BAK_0_TEXT", "BAK_0_MEDIA"):
                (folder / name).write_bytes(b"fixture")
            other = self.b / dirname
            other.mkdir()
            for path in folder.iterdir():
                os.link(path, other / path.name)
        (self.b / "extra.ini").write_bytes(b"extra")
        # A source alias is not an additional object.
        os.link(self.a / "set-one/BAK_0_MEDIA", self.a / "alias")
        job = self.scan()
        report = analyze_inventory(self.manager, job)
        self.assertEqual(len(report["backups"]), 2)  # other is recognized by its structure
        self.assertEqual(report["summary"]["directory_groups"], 1)
        self.assertEqual(report["directory_groups"][0]["backup_set_count"], 2)
        self.assertEqual(report["directory_groups"][0]["representative_source_id"], 2)
        pair = report["shared_sources"][0]
        self.assertEqual((pair["a_unshared_objects"], pair["b_unshared_objects"]), (0, 1))
        profile = next(b for b in report["backups"] if b["source_id"] == 2)
        self.assertEqual(profile["object_coverage"]["not_observed_shared_logical_bytes"], 5)
        self.assertEqual(
            saved_files(self.manager, job, 2, sharing="not_observed_shared")["items"][0]["path"],
            "extra.ini",
        )
        with self.manager._connect(job) as db:
            db.execute("UPDATE job SET status='partial'")
        self.assertEqual(len(analyze_inventory(self.manager, job)["directory_groups"]), 2)

    def test_directory_structure_is_a_candidate_not_decoded_content(self):
        (self.a / "account/Index").mkdir(parents=True)
        (self.a / "account/backup.attr").write_bytes(b"opaque synthetic metadata")
        job = self.scan()
        profile = analyze_inventory(self.manager, job)["backups"][0]
        self.assertEqual(profile["structure"]["family"], "segmented_backup_candidate")
        self.assertFalse(profile["structure"]["contents_verified"])
        with self.manager._connect(job) as db:
            db.execute("UPDATE job SET status='running'")
        with self.assertRaises(ValueError):
            saved_files(self.manager, job)
        with self.assertRaises(ValueError):
            saved_files(StorageInventory(self.manager.directory, "other"), job)
