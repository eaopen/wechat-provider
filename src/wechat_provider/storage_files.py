"""Bounded inventory file browsing and identity-checked Finder locations."""

import os
import stat
from pathlib import Path, PurePosixPath

from .storage_analysis import FILES

KINDS = {"image", "video", "audio", "database", "archive", "document", "other"}
SHARED = """EXISTS (SELECT 1 FROM entries x WHERE x.source<>e.source
    AND x.dev=e.dev AND x.ino=e.ino AND x.size=e.size AND x.mtime=e.mtime
    AND x.kind NOT IN ('directory','symlink','special'))"""


def saved_files(
    manager, job_id, source_id=None, query="", file_kind=None, sharing="all", offset=0, limit=50
):
    if (
        source_id is not None
        and (type(source_id) is not int or source_id < 1)
        or not isinstance(query, str)
        or len(query.encode()) > 256
        or "\0" in query
        or file_kind not in (None, *KINDS)
        or sharing not in ("all", "shared", "not_observed_shared")
        or type(offset) is not int
        or not 0 <= offset <= 2000000
        or type(limit) is not int
        or not 1 <= limit <= 50
    ):
        raise ValueError("invalid inventory file filter")
    with manager._connect(job_id) as db:
        db.execute("BEGIN")
        job = db.execute("SELECT status,updated FROM job").fetchone()
        if job["status"] in ("running", "queued"):
            raise ValueError("finish or cancel inventory before browsing files")
        roots = [dict(r) for r in db.execute("SELECT id,kind,path,access FROM sources ORDER BY id")]
        if source_id is not None and not any(r["id"] == source_id for r in roots):
            raise ValueError("inventory source not found")
        where, args = ["e." + FILES], []
        if source_id is not None:
            where.append("e.source=?")
            args.append(source_id)
        if file_kind:
            where.append("e.kind=?")
            args.append(file_kind)
        if query:
            literal = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            where.append("e.path LIKE ? ESCAPE '\\'")
            args.append("%" + literal + "%")
        if sharing != "all":
            where.append(SHARED if sharing == "shared" else "NOT " + SHARED)
        filters = " AND ".join(where)
        total = db.execute("SELECT COUNT(*) FROM entries e WHERE " + filters, args).fetchone()[0]
        items = [
            dict(r)
            for r in db.execute(
                "SELECT e.source,e.path,e.kind,e.size,e.allocated,e.mtime,"
                + SHARED
                + " shared_with_other_source FROM entries e WHERE "
                + filters
                + " ORDER BY e.size DESC,e.source,e.path LIMIT ? OFFSET ?",
                (*args, limit, offset),
            )
        ]
    return {
        "files_version": 1,
        "job_id": job_id,
        "inventory_status": job["status"],
        "inventory_observed_at": job["updated"],
        "source_id": source_id,
        "sources": roots,
        "query": query,
        "file_kind": file_kind,
        "sharing": sharing,
        "offset": offset,
        "limit": limit,
        "total_file_paths": total,
        "has_more": offset + len(items) < total,
        "items": items,
        "scan_started": False,
        "source_contents_read": False,
        "source_mutations": False,
        "reclaimable_bytes": None,
    }


def locate_file(manager, job_id, source_id, entry_path):
    if (
        type(source_id) is not int
        or source_id < 1
        or not isinstance(entry_path, str)
        or not entry_path
        or len(entry_path.encode()) > 4096
        or "\0" in entry_path
        or PurePosixPath(entry_path).is_absolute()
        or ".." in PurePosixPath(entry_path).parts
    ):
        raise ValueError("invalid saved file location")
    with manager._connect(job_id) as db:
        root = db.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
        row = db.execute(
            "SELECT * FROM entries WHERE source=? AND path=? AND " + FILES, (source_id, entry_path)
        ).fetchone()
        parent = str(PurePosixPath(entry_path).parent)
        parent = "" if parent == "." else parent
        directory = db.execute(
            "SELECT dev,ino FROM dirs WHERE source=? AND path=?", (source_id, parent)
        ).fetchone()
        if root is None or row is None or directory is None:
            raise ValueError("saved file location not found")
        fd = manager._open_dir(root["path"], parent, (directory["dev"], directory["ino"]))
        try:
            info = os.stat(PurePosixPath(entry_path).name, dir_fd=fd, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode) or (
                info.st_dev,
                info.st_ino,
                info.st_size,
                info.st_mtime,
            ) != (row["dev"], row["ino"], row["size"], row["mtime"]):
                raise ValueError("saved file has changed; refresh inventory")
        finally:
            os.close(fd)
        return {
            "location_version": 1,
            "job_id": job_id,
            "source_id": source_id,
            "entry_path": entry_path,
            "absolute_path": str(Path(root["path"]) / entry_path),
            "identity_checked": True,
            "source_mutations": False,
        }
