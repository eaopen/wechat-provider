"""Recognize structural backup families in saved metadata, not encrypted contents."""

import re
from pathlib import PurePosixPath


def source_structure(db, source):
    candidates = db.execute(
        "SELECT path FROM entries WHERE source=? AND kind NOT IN ('directory','symlink','special') "
        "AND (path LIKE '%Backup.db' OR path LIKE '%BAK\\_%' ESCAPE '\\' "
        "OR path LIKE '%backup.attr') LIMIT 4097",
        (source["id"],),
    ).fetchall()
    limited = len(candidates) > 4096
    parents = {}
    attrs = []
    index_count = 0
    for row in candidates[:4096]:
        path = PurePosixPath(row[0])
        parent = str(path.parent)
        group = parents.setdefault(parent, {"index": False, "text": [], "media": []})
        if path.name == "Backup.db":
            group["index"] = True
            index_count += 1
        elif path.name == "backup.attr":
            attrs.append(str(path))
        elif match := re.fullmatch(r"BAK_(\d+)_(TEXT|MEDIA)", path.name):
            group["text" if match[2] == "TEXT" else "media"].append(int(match[1]))
    sets = [
        {
            "directory": parent,
            "text_containers": len(group["text"]),
            "media_containers": len(group["media"]),
        }
        for parent, group in sorted(parents.items())
        if group["index"] and 0 in group["text"]
    ]
    index_dirs = db.execute(
        "SELECT COUNT(*) FROM entries WHERE source=? AND kind='directory' "
        "AND (path='Index' OR path LIKE '%/Index')",
        (source["id"],),
    ).fetchone()[0]
    family = (
        "legacy_bak_containers"
        if sets
        else (
            "segmented_backup_candidate"
            if attrs and index_dirs
            else "backup_index_present"
            if index_count
            else "unrecognized_backup_directory"
        )
    )
    name = PurePosixPath(source["path"]).name
    label = (
        "迁移保留的旧版备份"
        if name == "old_backup" and sets
        else "旧版微信容器备份"
        if sets
        else "分目录备份位置"
        if family == "segmented_backup_candidate"
        else "备份位置"
    )
    return {
        "family": family,
        "label": label,
        "backup_sets": sets,
        "backup_index_files": index_count,
        "attribute_files": len(attrs),
        "index_directories": index_dirs,
        "limited": limited,
        "recognition": "directory_structure_only",
        "contents_verified": False,
    }


def directory_groups(profiles, pairs, inventory_status):
    """Group observed subset entrances only when both traversals finished cleanly."""
    parents = {p["source_id"]: p["source_id"] for p in profiles}
    by_id = {p["source_id"]: p for p in profiles}

    def parent(sid):
        while parents[sid] != sid:
            sid = parents[sid]
        return sid

    if inventory_status == "completed":
        for pair in pairs:
            a, b = pair["source_a"], pair["source_b"]
            if a not in parents or b not in parents:
                continue
            left, right = by_id[a], by_id[b]
            if (
                left["access"] != "readable"
                or right["access"] != "readable"
                or left["issue_count"]
                or right["issue_count"]
                or left["structure"]["family"] != right["structure"]["family"]
            ):
                continue
            if pair["shared_objects"] and pair["shared_objects"] in (
                left["unique_files"],
                right["unique_files"],
            ):
                parents[parent(b)] = parent(a)
    grouped = {}
    for profile in profiles:
        grouped.setdefault(parent(profile["source_id"]), []).append(profile)
    return [
        {
            "id": i,
            "source_ids": [p["source_id"] for p in items],
            "representative_source_id": max(
                items, key=lambda p: (p["unique_files"], p["unique_logical_bytes"] or 0)
            )["source_id"],
            "basis": "observed_identity_subset" if len(items) > 1 else "single_location",
            "backup_set_count": max(len(p["structure"]["backup_sets"]) for p in items),
        }
        for i, items in enumerate(grouped.values(), 1)
    ]
