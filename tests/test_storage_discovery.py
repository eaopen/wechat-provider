import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from wechat_provider.service import Service
from wechat_provider.storage_discovery import discover_sources
from wechat_provider.store import Store


class DiscoveryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.base = self.home / "Library/Containers/com.tencent.xinWeChat/Data"

    def folder(self, relative):
        path = self.base / relative
        path.mkdir(parents=True)
        return path

    def test_defaults_detect_accounts_backups_and_cache_without_full_scan(self):
        account = self.folder("Documents/xwechat_files/fixture-account/db_storage").parent
        backup = self.folder("Documents/xwechat_files/Backup")
        legacy = self.folder("Library/Application Support/com.tencent.xinWeChat/2.0b4.0.9/Backup")
        cache = self.folder("Library/Caches")
        (account / "do-not-read.txt").write_text("SYNTHETIC_PRIVATE_CONTENT")
        with patch.object(Path, "open", side_effect=AssertionError("no content or key reads")):
            result = discover_sources(self.home)
        by_path = {s["path"]: s for s in result["sources"]}
        self.assertEqual(by_path[str(account)]["kind"], "active_data")
        self.assertEqual(by_path[str(backup)]["kind"], "official_backup")
        self.assertEqual(by_path[str(legacy)]["kind"], "official_backup")
        self.assertEqual(by_path[str(cache)]["kind"], "cache_downloads")
        self.assertTrue(all(s["selected"] for s in result["sources"]))
        self.assertFalse(result["scan_started"])
        self.assertFalse(result["coverage_complete"])
        self.assertNotIn("SYNTHETIC_PRIVATE_CONTENT", json.dumps(result))
        roots = list(map(Path, by_path))
        self.assertFalse(any(a != b and a.is_relative_to(b) for a in roots for b in roots))

    def test_permission_denial_stays_visible_and_unselected(self):
        root = self.folder("Documents/xwechat_files")
        original = os.scandir

        def denied(path):
            if Path(path) == root:
                raise PermissionError(13, "DO_NOT_ECHO")
            return original(path)

        with patch("wechat_provider.storage_discovery.os.scandir", side_effect=denied):
            result = discover_sources(self.home)
        self.assertEqual(result["sources"][0]["access"], "access_denied")
        self.assertEqual(result["selected_count"], 0)
        self.assertTrue(result["issues"])
        self.assertNotIn("DO_NOT_ECHO", json.dumps(result))

    def test_migrated_old_backup_is_not_hidden_among_other_sources(self):
        migrated = self.folder("Documents/xwechat_files/old_backup")
        result = discover_sources(self.home)
        source = next(s for s in result["sources"] if s["path"] == str(migrated))
        self.assertEqual(source["kind"], "official_backup")
        self.assertEqual(source["reason"], "wechat_migrated_backup_location")

    def test_no_home_wide_search_and_symlink_locations_are_not_followed(self):
        elsewhere = self.home / "private-unrelated/old-device/db_storage"
        elsewhere.mkdir(parents=True)
        current = self.base / "Documents/xwechat_files"
        current.parent.mkdir(parents=True)
        current.symlink_to(elsewhere.parent, target_is_directory=True)
        result = discover_sources(self.home)
        self.assertEqual(result["sources"], [])
        self.assertTrue(any(i["code"] == "symlink_location_not_followed" for i in result["issues"]))

    def test_existing_bound_custom_account_is_used_but_bad_config_cannot_select_home(self):
        state = self.home / "reader-state"
        state.mkdir()
        custom = self.home / "custom-storage/account"
        (custom / "db_storage").mkdir(parents=True)
        config = state / "config.json"
        config.write_text(json.dumps({"account_root": str(custom)}))
        result = discover_sources(self.home, state)
        self.assertEqual(result["sources"][0]["path"], str(custom))
        self.assertEqual(result["sources"][0]["reason"], "configured_reader_account")
        config.write_text(json.dumps({"account_root": str(self.home)}))
        result = discover_sources(self.home, state)
        self.assertEqual(result["sources"], [])
        self.assertTrue(result["issues"])

    def test_root_budget_and_duplicate_binding_are_explicit(self):
        current = self.folder("Documents/xwechat_files")
        for i in range(540):
            (current / f"fixture-{i}").mkdir()
        # Exercise the entry budget independently of machine speed.
        with patch("wechat_provider.storage_discovery.time.monotonic", return_value=1):
            result = discover_sources(self.home)
        self.assertLessEqual(result["checks_used"], 512)
        self.assertEqual(len(result["sources"]), 32)
        self.assertTrue(result["discovery_limited"])
        self.assertGreater(result["omitted_sources"], 0)

    def test_same_bound_and_standard_account_only_occurs_once(self):
        account = self.folder("Documents/xwechat_files/fixture-account/db_storage").parent
        state = self.home / "reader-state"
        state.mkdir()
        (state / "config.json").write_text(json.dumps({"account_root": str(account)}))
        result = discover_sources(self.home, state)
        self.assertEqual(len(result["sources"]), 1)
        self.assertEqual(result["sources"][0]["reason"], "configured_reader_account")

    def test_service_discovery_needs_no_scan_state_or_source_paths(self):
        from unittest.mock import Mock

        adapter = Mock()
        service = Service(Store(self.home / "index.db"), adapter)
        with patch(
            "wechat_provider.storage_discovery.discover_sources",
            return_value={"discovery_version": 1},
        ) as discover:
            self.assertEqual(service.storage_inventory("discover"), {"discovery_version": 1})
            discover.assert_called_once()
        self.assertEqual(adapter.mock_calls, [])
        self.assertFalse((self.home / "storage").exists())
        with self.assertRaises(ValueError):
            service.storage_inventory("discover", sources=[{"path": str(self.home)}])
