"""Bounded macOS WeChat source discovery; metadata only, no recursive home scan."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import time
from collections import Counter
from pathlib import Path

BACKUP_NAMES = {"Backup", "Backups", "BackupFiles", "backup", "backupFiles"}


def discover_sources(home=None, reader_state=None):
    home = Path(home) if home is not None else Path.home()
    base = home / "Library/Containers/com.tencent.xinWeChat/Data"
    found, issues, searched = [], [], []
    visited = set()
    remaining = 512
    deadline = time.monotonic() + 3
    limited = False

    def issue(location, code):
        if len(issues) < 20:
            issues.append({"location": str(location), "code": code})

    def probe(path):
        try:
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                issue(path, "symlink_location_not_followed")
                return "symlink"
            return "directory" if stat.S_ISDIR(info.st_mode) else "not_directory"
        except FileNotFoundError:
            return "missing"
        except OSError:
            issue(path, "access_denied_or_unavailable")
            return "blocked"

    def children(path):
        nonlocal remaining, limited
        if probe(path) != "directory":
            return []
        result = []
        try:
            with os.scandir(path) as entries:
                for entry in entries:
                    if remaining <= 0 or time.monotonic() > deadline:
                        limited = True
                        break
                    remaining -= 1
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            result.append(Path(entry.path))
                    except OSError:
                        issue(path, "some_entries_unavailable")
            return sorted(result, key=lambda p: p.name)
        except OSError:
            issue(path, "access_denied_or_unavailable")
            return []

    def add(path, kind, label, reason):
        nonlocal limited
        if time.monotonic() > deadline:
            limited = True
            return
        state = probe(path)
        if state in ("missing", "not_directory", "symlink"):
            return
        try:
            canonical = path.resolve() if state == "directory" else path.absolute()
        except OSError:
            canonical = path.absolute()
            state = "blocked"
        key = str(canonical)
        if key in visited:
            return
        # The caller receives independent roots accepted by the inventory engine.
        if any(
            canonical.is_relative_to(Path(s["path"])) or Path(s["path"]).is_relative_to(canonical)
            for s in found
        ):
            return
        access = "access_denied"
        if state == "directory":
            try:
                with os.scandir(canonical) as entries:
                    next(entries, None)  # verify listing, never inspect file contents
                access = "readable"
            except OSError:
                issue(path, "access_denied_or_unavailable")
        visited.add(key)
        found.append(
            {
                "path": key,
                "kind": kind,
                "label": label,
                "id": hashlib.sha256(key.encode()).hexdigest()[:20],
                "origin": "discovered",
                "reason": reason,
                "access": access,
                "selected": access == "readable",
                "recovery_status": "not_verified",
            }
        )

    # Already-bound custom account paths are explicit configuration, not a guess.
    # Only the non-secret config is read; keys and identity files are not opened.
    if reader_state:
        config = Path(reader_state).expanduser() / "config.json"
        try:
            if not config.is_symlink() and config.stat().st_size <= 65536:
                with config.open("rb") as stream:
                    data = json.loads(stream.read(65537))
                account = Path(data["account_root"])
                if account.is_absolute() and probe(account / "db_storage") == "directory":
                    add(account, "active_data", "已绑定的微信账号数据", "configured_reader_account")
                else:
                    issue(config.parent, "saved_reader_location_not_recognized")
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError, KeyError):
            issue(config.parent, "saved_reader_location_unavailable")

    current = base / "Documents/xwechat_files"
    searched.append(str(current))
    current_state = probe(current)
    dirs = children(current)
    if current_state == "blocked":
        add(current, "active_data", "微信 4.x 数据位置", "standard_wechat4_location")
    elif current_state == "directory":
        # Split at known account/shared/backup boundaries instead of selecting a
        # parent and its Backup child, which would double count the same tree.
        if not dirs:
            add(current, "active_data", "微信 4.x 数据位置", "standard_wechat4_location")
        for path in dirs:
            if path.name in BACKUP_NAMES:
                add(path, "official_backup", "微信 4.x 备份目录", "wechat_backup_location")
            elif path.name == "old_backup":
                add(path, "official_backup", "迁移保留的旧版备份", "wechat_migrated_backup_location")
            elif probe(path / "db_storage") == "directory":
                add(path, "active_data", "微信 4.x 聊天与附件", "wechat4_database_layout")
            elif path.name == "all_users":
                add(path, "other", "微信共享数据", "wechat_shared_location")
            else:
                add(path, "other", "微信其他本地数据", "wechat4_container_child")

    # Old client data and its backups are kept distinct from current 4.x sources.
    for legacy in (
        base / "Library/Application Support/com.tencent.xinWeChat",
        home / "Library/Application Support/com.tencent.xinWeChat",
    ):
        searched.append(str(legacy))
        if probe(legacy) == "blocked":
            add(legacy, "historical_copy", "旧版微信数据位置", "legacy_wechat_location")
        for version in children(legacy):
            if version.name in BACKUP_NAMES:
                add(version, "official_backup", "旧版微信备份目录", "wechat_backup_location")
            elif re.fullmatch(r"\d[\da-zA-Z._-]{1,64}", version.name):
                contents = children(version)
                if not contents:
                    add(version, "historical_copy", "旧版微信数据", "legacy_version_location")
                for path in contents:
                    if path.name in BACKUP_NAMES:
                        add(path, "official_backup", "旧版微信备份目录", "wechat_backup_location")
                    else:
                        add(path, "historical_copy", "旧版微信本地数据", "legacy_version_child")

    extras = (
        (base / "Documents/xwechat_files.tmp", "other", "微信迁移暂存数据"),
        (base / "Documents/cacheDir", "cache_downloads", "微信临时缓存"),
        (base / "Documents/Caches", "cache_downloads", "微信文档缓存"),
        (base / "Library/Caches", "cache_downloads", "微信应用缓存"),
    )
    for path, kind, label in extras:
        searched.append(str(path))
        add(path, kind, label, "standard_wechat_location")

    # Prefer actual account and backup locations when the scan's root
    # budget is exceeded. Remaining locations stay explicit as a discovery gap.
    rank = {
        "active_data": 0,
        "official_backup": 1,
        "historical_copy": 2,
        "other": 3,
        "cache_downloads": 4,
    }
    found.sort(key=lambda s: (rank[s["kind"]], s["access"] != "readable", s["path"]))
    omitted = max(0, len(found) - 32)
    found = found[:32]
    names = Counter(s["label"] for s in found)
    counts = Counter()
    for source in found:
        label = source["label"]
        if names[label] > 1:
            counts[label] += 1
            source["label"] = f"{label}（{counts[label]}）"
    return {
        "discovery_version": 1,
        "sources": found,
        "issues": issues,
        "selected_count": sum(s["selected"] for s in found),
        "searched_locations": searched,
        "scan_started": False,
        "coverage_complete": False,
        "discovery_limited": limited or omitted > 0,
        "omitted_sources": omitted,
        "checks_used": 512 - remaining,
        "limitations": [
            "known_wechat_locations_only",
            "custom_or_unmounted_backups_may_be_missing",
            "classification_is_location_based_not_restore_verification",
        ],
    }
