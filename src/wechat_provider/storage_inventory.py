"""Storage governance inventory, separate from future asset-operation executors.

P0 application policy exposes metadata inspection only. No file contents, keys,
GUI automation, source writes, media hydration, hashes or deletion are involved.
"""

from __future__ import annotations

import os
import sqlite3
import stat
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .locks import desktop_lock

SOURCE_KINDS = {"active_data", "official_backup", "historical_copy", "cache_downloads", "other"}
TERMINAL = {"completed", "partial", "cancelled", "limited", "failed"}


def capabilities():
    # Effects and policy belong to operations, not to the Provider as a whole.
    return {
        "contract_version": 1,
        "implemented_operations": ["inventory", "backup_analysis"],
        "application_policy": {
            "allowed_operations": ["inventory", "backup_analysis"], "source_mutations": False
        },
        "operation_effects": {
            "inventory": "read_source_metadata", "backup_analysis": "read_saved_inventory"
        },
        "asset_execution_available": False,
        "requires_database_keys": False,
        "source_discovery_version": 1,
        "file_browser_version": 1,
    }


def file_kind(name):
    suffix = Path(name).suffix.lower()
    groups = {
        "image": {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".dat"},
        "video": {".mp4", ".mov", ".m4v", ".avi", ".mkv"},
        "audio": {".mp3", ".wav", ".m4a", ".amr", ".silk", ".aac"},
        "database": {".db", ".sqlite", ".sqlite3", ".kvdb"},
        "archive": {".zip", ".7z", ".rar", ".tar", ".gz"},
        "document": {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".txt"},
    }
    return next((kind for kind, endings in groups.items() if suffix in endings), "other")


class StorageInventory:
    def __init__(self, directory: Path, account_key="default"):
        selected = Path(directory).expanduser().absolute()
        if selected.is_symlink():
            raise ValueError("symlink inventory state is unsupported")
        # macOS /var and /tmp are benign system aliases. Bind their canonical
        # parent before creating any state, while rejecting a symlink state root.
        self.directory = selected.resolve()
        self.account_key = account_key

    def _path(self, job_id):
        try:
            if str(uuid.UUID(job_id)) != job_id:
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise ValueError("invalid storage job ID") from None
        path = self.directory / job_id / "inventory.db"
        if not path.is_file() or path.is_symlink() or path.parent.is_symlink():
            raise ValueError("storage job not found")
        return path

    @contextmanager
    def _connect(self, job_id):
        db = sqlite3.connect(self._path(job_id), timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                owner = db.execute("SELECT account_key FROM job").fetchone()
                if not owner or owner[0] != self.account_key:
                    raise ValueError("storage job not found")
                yield db
        finally:
            db.close()

    def start(self, sources, max_entries=250000, launch=True):
        if os.name != "posix" or not hasattr(os, "O_NOFOLLOW"):
            raise ValueError("storage inventory currently requires POSIX")
        if not isinstance(sources, list) or not 1 <= len(sources) <= 32:
            raise ValueError("choose 1-32 source directories")
        if type(max_entries) is not int or not 1 <= max_entries <= 2000000:
            raise ValueError("max_entries must be 1-2000000")
        normalized = []
        state = self.directory.resolve()
        for source in sources:
            if (
                not isinstance(source, dict)
                or source.get("kind") not in SOURCE_KINDS
                or not isinstance(source.get("path"), str)
                or not source["path"]
                or "\0" in source["path"]
                or len(source["path"].encode()) > 4096
            ):
                raise ValueError("invalid source directory or kind")
            path = Path(source["path"]).expanduser()
            if not path.is_absolute() or path.is_symlink():
                raise ValueError("source must be an absolute non-symlink directory")
            # Resolve before comparisons; no source directory is created here.
            path = path.resolve()
            if state == path or state.is_relative_to(path) or path.is_relative_to(state):
                raise ValueError("inventory state must be outside the selected sources")
            if any(
                path == old or path.is_relative_to(old) or old.is_relative_to(path)
                for old, _ in normalized
            ):
                raise ValueError("selected directories overlap; choose each source once")
            normalized.append((path, source["kind"]))
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if any(p.is_symlink() for p in (self.directory, *self.directory.parents)):
            raise ValueError("symlink inventory state is unsupported")
        if self.directory.stat().st_mode & 0o077:
            raise ValueError("inventory state directory must be private (0700)")
        job_id = str(uuid.uuid4())
        folder = self.directory / job_id
        folder.mkdir(mode=0o700)
        path = folder / "inventory.db"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
        with sqlite3.connect(path) as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE job(account_key TEXT,status TEXT,cancel INTEGER,created REAL,
                    updated REAL,max_entries INTEGER,worker_pid INTEGER,resumes INTEGER DEFAULT 0);
                CREATE TABLE sources(id INTEGER PRIMARY KEY,path TEXT,kind TEXT,dev INTEGER,ino INTEGER,
                    access TEXT DEFAULT 'not_checked');
                CREATE TABLE dirs(source INTEGER,path TEXT,dev INTEGER,ino INTEGER,depth INTEGER,
                    done INTEGER DEFAULT 0,PRIMARY KEY(source,path));
                CREATE TABLE entries(source INTEGER,path TEXT,kind TEXT,dev INTEGER,ino INTEGER,
                    size INTEGER,allocated INTEGER,nlink INTEGER,mtime REAL,
                    PRIMARY KEY(source,path));
                CREATE INDEX object_identity ON entries(dev,ino);
                CREATE TABLE issues(source INTEGER,path TEXT,code TEXT,
                    UNIQUE(source,path,code));
            """)
            now = time.time()
            db.execute(
                "INSERT INTO job VALUES (?,'queued',0,?,?,?,NULL,0)",
                (self.account_key, now, now, max_entries),
            )
            for i, (root, kind) in enumerate(normalized, 1):
                try:
                    info = root.stat()
                    if not stat.S_ISDIR(info.st_mode):
                        raise NotADirectoryError()
                    dev, ino = info.st_dev, info.st_ino
                except OSError:
                    dev = ino = None
                db.execute(
                    "INSERT INTO sources(id,path,kind,dev,ino) VALUES (?,?,?,?,?)",
                    (i, str(root), kind, dev, ino),
                )
                db.execute(
                    "INSERT INTO dirs(source,path,dev,ino,depth) VALUES (?, '',?,?,0)",
                    (i, dev, ino),
                )
        if launch:
            self._launch(job_id)
        return self.report(job_id)

    def _launch(self, job_id):
        try:
            subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "wechat_provider.storage_worker",
                    "--directory",
                    str(self.directory),
                    "--account",
                    self.account_key,
                    "--job",
                    job_id,
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError:
            with self._connect(job_id) as db:
                db.execute("UPDATE job SET status='failed',updated=?", (time.time(),))
            raise ValueError("inventory worker could not start") from None

    def cancel(self, job_id):
        with self._connect(job_id) as db:
            db.execute("UPDATE job SET cancel=1 WHERE status IN ('running','queued')")
        return self.report(job_id)

    def resume(self, job_id, max_entries=250000, launch=True):
        if type(max_entries) is not int or not 1 <= max_entries <= 2000000:
            raise ValueError("max_entries must be 1-2000000")
        with self._connect(job_id) as db:
            row = db.execute("SELECT status,updated FROM job").fetchone()
            if row[0] in ("completed", "partial"):
                raise ValueError("finished inventory requires a new scan")
            if row[0] in ("running", "queued") and time.time() - row[1] < 30:
                raise ValueError("inventory is still active")
            db.execute(
                "UPDATE job SET cancel=0,status='queued',updated=?,max_entries=?,resumes=resumes+1",
                (time.time(), max_entries),
            )
        if launch:
            self._launch(job_id)
        return self.report(job_id)

    @staticmethod
    def _issue(db, source, path, code):
        db.execute("INSERT OR IGNORE INTO issues VALUES (?,?,?)", (source, path, code))

    @staticmethod
    def _open_dir(root, relative, expected):
        # Walk from a source fd with O_NOFOLLOW for every component. A renamed
        # directory or a raced-in symlink cannot redirect the traversal.
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for name in Path(relative).parts:
                new = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = new
            info = os.fstat(fd)
            if (info.st_dev, info.st_ino) != expected:
                raise OSError("directory identity changed")
            return fd
        except BaseException:
            os.close(fd)
            raise

    def run(self, job_id, time_budget=120):
        """Durable directory queue; interruption replays the unfinished directory.

        Per-path primary keys prevent double counting after restart. Results are
        observations of a changing filesystem, never a consistent backup.
        """
        lock = self._path(job_id).with_suffix(".lock")
        with desktop_lock(lock), self._connect(job_id) as db:
            job = db.execute("SELECT * FROM job").fetchone()
            if job["status"] not in ("queued", "running"):
                return
            db.execute(
                "UPDATE job SET status='running',worker_pid=?,updated=?", (os.getpid(), time.time())
            )
            db.commit()
            started = time.monotonic()
            seen = db.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
            roots = {r["id"]: r for r in db.execute("SELECT * FROM sources")}
            while True:
                job = db.execute("SELECT * FROM job").fetchone()
                if job["cancel"]:
                    final = "cancelled"
                    break
                if seen >= job["max_entries"] or time.monotonic() - started >= time_budget:
                    final = "limited"
                    break
                directory = db.execute(
                    "SELECT * FROM dirs WHERE done=0 ORDER BY depth,source,path LIMIT 1"
                ).fetchone()
                if directory is None:
                    final = (
                        "partial"
                        if db.execute("SELECT COUNT(*) FROM issues").fetchone()[0]
                        else "completed"
                    )
                    break
                source, relative = directory["source"], directory["path"]
                root = roots[source]
                fd = None
                finished = True
                try:
                    fd = self._open_dir(
                        root["path"], relative, (directory["dev"], directory["ino"])
                    )
                    if not relative:
                        db.execute("UPDATE sources SET access='readable' WHERE id=?", (source,))
                    before = os.fstat(fd).st_mtime_ns
                    with os.scandir(fd) as entries:
                        for index, entry in enumerate(entries):
                            if seen >= job["max_entries"]:
                                finished = False
                                break
                            if index % 100 == 0:
                                db.execute("UPDATE job SET updated=?", (time.time(),))
                                db.commit()
                                control = db.execute("SELECT cancel FROM job").fetchone()[0]
                                if (
                                    control
                                    or seen >= job["max_entries"]
                                    or time.monotonic() - started >= time_budget
                                ):
                                    finished = False
                                    break
                            name = str(Path(relative) / entry.name)
                            if db.execute(
                                "SELECT 1 FROM entries WHERE source=? AND path=?", (source, name)
                            ).fetchone():
                                continue
                            try:
                                info = entry.stat(follow_symlinks=False)
                            except OSError as error:
                                self._issue(
                                    db,
                                    source,
                                    name,
                                    "access_denied"
                                    if error.errno in (1, 13)
                                    else "entry_unavailable",
                                )
                                continue
                            if stat.S_ISLNK(info.st_mode):
                                kind = "symlink"
                            elif stat.S_ISDIR(info.st_mode):
                                kind = "directory"
                                if info.st_dev != root["dev"]:
                                    self._issue(db, source, name, "mount_boundary_skipped")
                                elif directory["depth"] >= 64:
                                    self._issue(db, source, name, "depth_limit")
                                else:
                                    db.execute(
                                        "INSERT OR IGNORE INTO dirs(source,path,dev,ino,depth) "
                                        "VALUES (?,?,?,?,?)",
                                        (
                                            source,
                                            name,
                                            info.st_dev,
                                            info.st_ino,
                                            directory["depth"] + 1,
                                        ),
                                    )
                            elif stat.S_ISREG(info.st_mode):
                                kind = file_kind(entry.name)
                            else:
                                kind = "special"
                            db.execute(
                                "INSERT INTO entries VALUES (?,?,?,?,?,?,?,?,?)",
                                (
                                    source,
                                    name,
                                    kind,
                                    info.st_dev,
                                    info.st_ino,
                                    info.st_size,
                                    info.st_blocks * 512 if hasattr(info, "st_blocks") else None,
                                    info.st_nlink,
                                    info.st_mtime,
                                ),
                            )
                            seen += 1
                    if os.fstat(fd).st_mtime_ns != before:
                        self._issue(db, source, relative, "directory_changed_during_scan")
                except OSError as error:
                    self._issue(
                        db,
                        source,
                        relative,
                        "access_denied"
                        if error.errno in (1, 13)
                        else "directory_unavailable_or_changed",
                    )
                finally:
                    if fd is not None:
                        os.close(fd)
                if finished:
                    db.execute(
                        "UPDATE dirs SET done=1 WHERE source=? AND path=?", (source, relative)
                    )
                db.commit()
            db.execute("UPDATE job SET status=?,updated=?", (final, time.time()))

    @staticmethod
    def _totals(db, source=None):
        where, args = "kind NOT IN ('directory','symlink','special')", []
        if source is not None:
            where += " AND source=?"
            args.append(source)
        observed = db.execute(
            "SELECT COUNT(*) n,COALESCE(SUM(size),0) size FROM entries WHERE " + where, args
        ).fetchone()
        unique = db.execute(
            "SELECT COUNT(*) n,COALESCE(SUM(size),0) size,"
            "CASE WHEN COUNT(*)=COUNT(allocated) THEN COALESCE(SUM(allocated),0) END allocated,"
            "COALESCE(SUM(external),0) external FROM (SELECT MAX(size) size,"
            "MAX(allocated) allocated,MAX(nlink)>COUNT(*) external FROM entries WHERE "
            + where
            + " GROUP BY dev,ino)",
            args,
        ).fetchone()
        return {
            "file_paths": observed["n"],
            "logical_bytes": observed["size"],
            "unique_files": unique["n"],
            "unique_logical_bytes": unique["size"],
            "allocated_bytes_estimate": unique["allocated"],
            "hardlink_aliases": observed["n"] - unique["n"],
            "objects_with_links_outside_scope": unique["external"],
            "reclaimable_bytes": None,
        }

    def report(self, job_id):
        with self._connect(job_id) as db:
            db.execute("BEGIN")
            job = dict(db.execute("SELECT * FROM job").fetchone())
            sources = []
            for root in db.execute("SELECT * FROM sources ORDER BY id").fetchall():
                count = db.execute(
                    "SELECT COUNT(*) FROM issues WHERE source=?", (root["id"],)
                ).fetchone()[0]
                root_error = db.execute(
                    "SELECT code FROM issues WHERE source=? AND path='' LIMIT 1", (root["id"],)
                ).fetchone()
                access = (
                    "access_denied"
                    if root_error and root_error[0] == "access_denied"
                    else "unavailable"
                    if root_error and root_error[0] == "directory_unavailable_or_changed"
                    else "partial"
                    if count
                    else root["access"]
                )
                amounts = self._totals(db, root["id"])
                if access in ("not_checked", "access_denied", "unavailable"):
                    for key in (
                        "logical_bytes",
                        "unique_logical_bytes",
                        "allocated_bytes_estimate",
                    ):
                        amounts[key] = None
                sources.append(
                    {
                        "id": root["id"],
                        "path": root["path"],
                        "kind": root["kind"],
                        "access": access,
                        "recovery_status": "not_verified",
                        "issue_count": count,
                        **amounts,
                    }
                )
            issues = [dict(r) for r in db.execute("SELECT source,path,code FROM issues LIMIT 10")]
            issue_count = db.execute("SELECT COUNT(*) FROM issues").fetchone()[0]
            top = [
                dict(r)
                for r in db.execute(
                    "SELECT source,path,size,allocated,kind FROM entries WHERE kind NOT IN "
                    "('directory','symlink','special') ORDER BY size DESC,source,path LIMIT 20"
                )
            ]
            categories = [
                dict(r)
                for r in db.execute(
                    "SELECT kind,COUNT(*) file_paths,SUM(size) logical_bytes "
                    "FROM entries WHERE kind NOT IN ('directory','symlink','special') "
                    "GROUP BY kind ORDER BY SUM(size) DESC"
                )
            ]
            skipped = db.execute(
                "SELECT COUNT(*) FROM entries WHERE kind IN ('symlink','special')"
            ).fetchone()[0]
            pending = db.execute("SELECT COUNT(*) FROM dirs WHERE done=0").fetchone()[0]
            total = self._totals(db)
            if all(
                source["access"] in ("not_checked", "access_denied", "unavailable")
                for source in sources
            ):
                for key in ("logical_bytes", "unique_logical_bytes", "allocated_bytes_estimate"):
                    total[key] = None
            total["measurement_status"] = (
                "observed_scope" if job["status"] == "completed" else "partial_or_pending"
            )
            entries = db.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
        stale = job["status"] in ("running", "queued") and time.time() - job["updated"] > 30
        return {
            "contract_version": 1,
            "job_id": job_id,
            "status": job["status"],
            "worker_stale": stale,
            "cancel_requested": bool(job["cancel"]),
            "observed_at": job["updated"],
            "resumes": job["resumes"],
            "processed_entries": entries,
            "max_entries": job["max_entries"],
            "pending_directories": pending,
            "traversal_finished": job["status"] in ("completed", "partial"),
            "coverage_complete": False,
            "consistency": "live_non_atomic_metadata",
            "sources": sources,
            "totals": total,
            "categories": categories,
            "largest_files": top,
            "issues": issues,
            "issue_count": issue_count,
            "skipped_links_and_special": skipped,
            "application_policy": capabilities()["application_policy"],
            "limitations": [
                "allocated_bytes_estimate_does_not_deduplicate_APFS_clones",
                "snapshots_and_Time_Machine_not_measured",
                "backup_recovery_not_verified",
                "file_type_from_extension_only",
                "no_content_duplicate_detection",
                "selected_roots_only_no_symlink_or_mount_traversal",
            ],
        }
