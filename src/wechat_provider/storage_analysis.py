"""Backup triage from a saved inventory, without opening or rescanning sources.

File identities establish observed hard-link sharing, never content coverage or
restore suitability. All timestamps are file mtimes, not message/backup dates.
"""

from __future__ import annotations

import time
from pathlib import PurePosixPath

from .storage_structure import directory_groups, source_structure

FILES = "kind NOT IN ('directory','symlink','special')"


def analyze_inventory(manager, job_id):
    with manager._connect(job_id) as db:
        db.execute("BEGIN")
        job = db.execute("SELECT status,updated FROM job").fetchone()
        if job["status"] in ("queued", "running"):
            raise ValueError("finish or cancel the inventory before analyzing it")
        roots = [dict(r) for r in db.execute("SELECT * FROM sources ORDER BY id")]
        structures = {
            r["id"]: source_structure(db, r)
            for r in roots
            if r["kind"] in ("official_backup", "historical_copy", "other")
        }
        backups = [
            r
            for r in roots
            if r["kind"] == "official_backup"
            or (
                r["id"] in structures
                and structures[r["id"]]["family"]
                in ("legacy_bak_containers", "segmented_backup_candidate")
            )
        ]
        # Group within each source first, so aliases do not inflate sharing.
        # Changed identities are not used as affirmative sharing evidence.
        shared = [
            dict(r)
            for r in db.execute(
                "WITH members AS (SELECT source,dev,ino FROM entries WHERE "
                + FILES
                + " GROUP BY source,dev,ino), objects AS (SELECT dev,ino,MAX(size) size "
                "FROM entries WHERE " + FILES + " GROUP BY dev,ino "
                "HAVING MIN(size)=MAX(size) AND MIN(mtime)=MAX(mtime)) "
                "SELECT a.source source_a,b.source source_b,COUNT(*) shared_objects,"
                "SUM(o.size) shared_logical_bytes FROM members a JOIN members b "
                "ON a.dev=b.dev AND a.ino=b.ino AND a.source<b.source "
                "JOIN objects o ON o.dev=a.dev AND o.ino=a.ino "
                "GROUP BY a.source,b.source ORDER BY a.source,b.source"
            )
        ]
        backup_ids = {r["id"] for r in backups}
        shared = [r for r in shared if r["source_a"] in backup_ids or r["source_b"] in backup_ids]
        shared_totals = {
            r["source"]: dict(r)
            for r in db.execute(
                "WITH objects AS (SELECT dev,ino,MAX(size) size FROM entries WHERE "
                + FILES
                + " GROUP BY dev,ino HAVING MIN(size)=MAX(size) AND MIN(mtime)=MAX(mtime) "
                "AND COUNT(DISTINCT source)>1), members AS (SELECT source,dev,ino FROM entries "
                "WHERE " + FILES + " GROUP BY source,dev,ino) SELECT m.source,COUNT(*) objects,"
                "SUM(o.size) logical_bytes FROM members m JOIN objects o ON m.dev=o.dev AND m.ino=o.ino "
                "GROUP BY m.source"
            )
        }
        profiles = []
        for root in backups:
            sid = root["id"]
            totals = manager._totals(db, sid)
            issues = db.execute("SELECT COUNT(*) FROM issues WHERE source=?", (sid,)).fetchone()[0]
            dates = db.execute(
                "SELECT MIN(mtime),MAX(mtime) FROM entries WHERE source=? AND " + FILES,
                (sid,),
            ).fetchone()
            markers = db.execute(
                "SELECT path FROM entries WHERE source=? AND "
                + FILES
                + " AND (path='Backup.db' OR path LIKE '%/Backup.db')",
                (sid,),
            ).fetchall()
            # SQLite LIKE is case-insensitive by default; require the exact marker.
            indexes = [r[0] for r in markers if PurePosixPath(r[0]).name == "Backup.db"]
            large = db.execute(
                "SELECT COUNT(*),COALESCE(SUM(size),0) FROM entries WHERE source=? AND "
                + FILES
                + " AND size>=1073741824",
                (sid,),
            ).fetchone()
            access = "partial" if issues and root["access"] == "readable" else root["access"]
            available = access in ("readable", "partial")
            if not available:
                for key in ("logical_bytes", "unique_logical_bytes", "allocated_bytes_estimate"):
                    totals[key] = None
            relations = [r for r in shared if sid in (r["source_a"], r["source_b"])]
            shared_amount = shared_totals.get(sid, {"objects": 0, "logical_bytes": 0})
            profiles.append(
                {
                    "source_id": sid,
                    "path": root["path"],
                    "access": access,
                    "issue_count": issues,
                    "label": structures[sid]["label"],
                    "structure": structures[sid],
                    "object_coverage": {
                        "shared_objects": shared_amount["objects"],
                        "not_observed_shared_objects": totals["unique_files"]
                        - shared_amount["objects"]
                        if available
                        else None,
                        "not_observed_shared_logical_bytes": totals["unique_logical_bytes"]
                        - shared_amount["logical_bytes"]
                        if available
                        else None,
                    },
                    **totals,
                    "file_mtime_range": {
                        "earliest": dates[0],
                        "latest": dates[1],
                        "meaning": "observed_file_modification_times_only",
                    },
                    "layout": "backup_index_present"
                    if indexes
                    else "unrecognized_backup_directory",
                    "backup_index_files": len(indexes),
                    "large_files": large[0],
                    "large_file_logical_bytes": large[1],
                    "sharing": relations,
                    "content_coverage": "not_checked",
                    "account_match": "not_verified",
                    "backup_version": "not_verified",
                    "recovery_status": "not_verified",
                    "recommendation": "retain_pending_verification"
                    if available
                    else "resolve_access_first",
                    "next_check": "compatible_index_reader"
                    if indexes
                    else "identify_backup_format",
                }
            )
        profiles.sort(
            key=lambda p: (p["logical_bytes"] is None, -(p["logical_bytes"] or 0), p["source_id"])
        )
        for rank, profile in enumerate(profiles, 1):
            profile["priority"] = rank
        by_id = {p["source_id"]: p for p in profiles}
        for pair in shared:
            for side in ("a", "b"):
                profile = by_id.get(pair["source_" + side])
                if profile:
                    pair[side + "_unshared_objects"] = (
                        profile["unique_files"] - pair["shared_objects"]
                    )
                    pair[side + "_unshared_logical_bytes"] = (
                        profile["unique_logical_bytes"] - pair["shared_logical_bytes"]
                        if profile["unique_logical_bytes"] is not None
                        else None
                    )
        groups = directory_groups(profiles, shared, job["status"])
        # A shared object can occur in several pairs: never add pair totals.
        ids = tuple(backup_ids)
        if ids:
            where = "source IN (" + ",".join("?" for _ in ids) + ") AND " + FILES
            summary = dict(
                db.execute(
                    "SELECT COUNT(*) unique_files,COALESCE(SUM(size),0) unique_logical_bytes,"
                    "CASE WHEN COUNT(*)=COUNT(allocated) THEN COALESCE(SUM(allocated),0) END "
                    "allocated_bytes_estimate FROM (SELECT MAX(size) size,MAX(allocated) allocated "
                    "FROM entries WHERE " + where + " GROUP BY dev,ino)",
                    ids,
                ).fetchone()
            )
        else:
            summary = {"unique_files": 0, "unique_logical_bytes": 0, "allocated_bytes_estimate": 0}
        unavailable = sum(p["access"] not in ("readable", "partial") for p in profiles)
        if profiles and unavailable == len(profiles):
            summary["unique_logical_bytes"] = summary["allocated_bytes_estimate"] = None
        summary.update(
            {
                "backup_sources": len(profiles),
                "directory_groups": len(groups),
                "unavailable_sources": unavailable,
                "logical_bytes": sum(p["logical_bytes"] or 0 for p in profiles)
                if not unavailable
                else None,
                "measurement_status": "observed_scope"
                if job["status"] == "completed"
                else "partial_inventory",
                "reclaimable_bytes": None,
            }
        )
    return {
        "analysis_version": 1,
        "job_id": job_id,
        "status": "analyzed",
        "analysis_kind": "saved_inventory_metadata",
        "inventory_status": job["status"],
        "inventory_observed_at": job["updated"],
        "analyzed_at": time.time(),
        "scan_started": False,
        "source_contents_read": False,
        "source_mutations": False,
        "coverage_complete": False,
        "content_overlap": "not_checked",
        "recovery_status": "not_verified",
        "reclaimable_bytes": None,
        "summary": summary,
        "backups": profiles,
        "directory_groups": groups,
        "shared_sources": shared,
        "limitations": [
            "file_mtimes_are_not_backup_or_chat_dates",
            "same_inode_is_observed_sharing_not_content_coverage",
            "different_inodes_do_not_prove_unique_content",
            "backup_format_account_version_and_restore_not_verified",
            "saved_non_atomic_inventory_not_current_source_verification",
            "APFS_clones_and_snapshots_not_deduplicated",
        ],
    }
