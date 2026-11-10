"""Explicit selected-file archives. Local job state; source files are never mutated.

Resume is at file boundaries. A verified copy is not a verified WeChat recovery
point, and neither metadata overlap nor copy success authorizes source deletion.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

from .storage_files import locate_file


@contextmanager
def _lock(path):
    import fcntl
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise TimeoutError('archive task active') from None
        yield
    finally:
        os.close(fd)


def _private_json(path, value):
    temp = path.with_name(path.name + '.' + str(uuid.uuid4()) + '.tmp')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as output:
        json.dump(value, output, ensure_ascii=False)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temp, path)


def _directory(path):
    """Open an existing absolute directory without traversing symlinks."""
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('absolute archive destination required')
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _child(fd, name):
    try:
        os.mkdir(name, mode=0o700, dir_fd=fd)
    except FileExistsError:
        pass
    return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)


def _hash(fd):
    value = hashlib.sha256()
    os.lseek(fd, 0, os.SEEK_SET)
    while chunk := os.read(fd, 1024 * 1024):
        value.update(chunk)
    return value.hexdigest()


class ArchiveManager:
    def __init__(self, inventory):
        self.inventory = inventory
        self.directory = inventory.directory.parent / 'vault-archive-jobs'
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.directory.is_symlink() or self.directory.stat().st_mode & 0o077:
            raise ValueError('archive state must be private')

    def _path(self, job_id):
        if not isinstance(job_id, str) or str(uuid.UUID(job_id)) != job_id:
            raise ValueError('invalid archive job')
        return self.directory / (job_id + '.json')

    def _load(self, job_id):
        path = self._path(job_id)
        if path.is_symlink():
            raise ValueError('unsafe archive job')
        job = json.loads(path.read_text())
        if job['account_key'] != self.inventory.account_key:
            raise ValueError('archive job not found')
        return job

    def plan(self, inventory_job, source_id, entries, destination):
        if (type(source_id) is not int or not isinstance(entries, list)
                or not 1 <= len(entries) <= 50 or len(set(entries)) != len(entries)):
            raise ValueError('select 1-50 distinct inventory files')
        fd = _directory(destination)
        try:
            identity = os.fstat(fd)
            space = os.fstatvfs(fd)
        finally:
            os.close(fd)
        target = Path(destination)
        with self.inventory._connect(inventory_job) as db:
            if db.execute('SELECT status FROM job').fetchone()[0] not in (
                    'completed', 'partial', 'cancelled', 'limited', 'failed'):
                raise ValueError('finish inventory before archiving')
            roots = [Path(row[0]) for row in db.execute('SELECT path FROM sources')]
            source_root = db.execute('SELECT path FROM sources WHERE id=?', (source_id,)).fetchone()
            if source_root is None:
                raise ValueError('inventory source not found')
        if any(target == root or target.is_relative_to(root) or root.is_relative_to(target)
               for root in roots):
            raise ValueError('archive destination overlaps inventory sources')
        if any(target == state or target.is_relative_to(state) or state.is_relative_to(target)
               for state in (self.inventory.directory, self.directory)):
            raise ValueError('archive destination overlaps local state')
        files = []
        for entry in entries:
            location = locate_file(self.inventory, inventory_job, source_id, entry)
            info = os.stat(location['absolute_path'], follow_symlinks=False)
            files.append({'path': entry, 'size': info.st_size, 'dev': info.st_dev,
                          'ino': info.st_ino, 'mtime_ns': info.st_mtime_ns,
                          'status': 'pending'})
        required = sum(item['size'] for item in files)
        if required + 1024 * 1024 > space.f_bavail * space.f_frsize:
            raise ValueError('insufficient archive space')
        job_id = str(uuid.uuid4())
        job = {'archive_version': 1, 'job_id': job_id, 'account_key': self.inventory.account_key,
               'inventory_job': inventory_job, 'source_id': source_id,
               'source_root': source_root[0],
               'destination': str(target), 'destination_dev': identity.st_dev,
               'destination_ino': identity.st_ino, 'status': 'planned', 'files': files,
               'created_at': time.time(), 'updated_at': time.time(),
               'source_mutations': False, 'recovery_status': 'not_verified'}
        _private_json(self._path(job_id), job)
        return self.report(job_id)

    def report(self, job_id):
        job = self._load(job_id)
        active = False
        if job['status'] in ('queued', 'running'):
            try:
                with _lock(self.directory / (job_id + '.lock')):
                    pass
            except TimeoutError:
                active = True
        return {key: job[key] for key in (
            'archive_version', 'job_id', 'inventory_job', 'source_id', 'destination',
            'status', 'files', 'created_at', 'updated_at', 'source_mutations', 'recovery_status'
        )} | {'total_bytes': sum(f['size'] for f in job['files']),
             'verified_files': sum(f['status'] == 'verified' for f in job['files']),
             'worker_active': active,
             'destination_online': self._online(job), 'resume_granularity': 'file'}

    @staticmethod
    def _online(job):
        try:
            fd = _directory(job['destination'])
            try:
                info = os.fstat(fd)
                return (info.st_dev, info.st_ino) == (
                    job['destination_dev'], job['destination_ino'])
            finally:
                os.close(fd)
        except OSError:
            return False

    def start(self, job_id, launch=True):
        self._path(job_id)
        with _lock(self.directory / (job_id + '.lock')):
            job = self._load(job_id)
            if not self._online(job):
                raise ValueError('archive destination offline or replaced')
            if job['status'] == 'completed':
                return self.report(job_id)
            (self.directory / (job_id + '.cancel')).unlink(missing_ok=True)
            job['status'] = 'queued'
            _private_json(self._path(job_id), job)
            if launch:
                worker = subprocess.Popen([
                    sys.executable, '-m', 'wechat_provider.vault_archive',
                    str(self.inventory.directory), self.inventory.account_key, job_id,
                ], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, start_new_session=True)
                threading.Thread(target=worker.wait, daemon=True).start()
        return self.report(job_id)

    def cancel(self, job_id):
        self._load(job_id)
        fd = os.open(self.directory / (job_id + '.cancel'),
                     os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        return self.report(job_id)

    def locate(self, job_id, index):
        job = self._load(job_id)
        if type(index) is not int or not 0 <= index < len(job['files']):
            raise ValueError('invalid archive file index')
        item = job['files'][index]
        if item['status'] != 'verified' or not self._online(job):
            raise ValueError('archive file not verified or destination offline')
        folder = Path(job['destination']) / 'WeChatVault' / 'media'
        fd = _directory(folder)
        try:
            source = os.open(item['sha256'], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            try:
                if not stat.S_ISREG(os.fstat(source).st_mode) or _hash(source) != item['sha256']:
                    raise ValueError('archive file checksum changed')
            finally:
                os.close(source)
        finally:
            os.close(fd)
        return {'vault_version': 1, 'job_id': job_id, 'identity_checked': True,
                'source_mutations': False, 'absolute_path': str(folder / item['sha256'])}

    def run(self, job_id):
        self._path(job_id)
        with _lock(self.directory / (job_id + '.lock')):
            job = self._load(job_id)
            job['status'] = 'running'
            _private_json(self._path(job_id), job)
            try:
                for item in job['files']:
                    if (self.directory / (job_id + '.cancel')).exists():
                        job['status'] = 'cancelled'
                        break
                    # Rehash archived files on resume, including verified checkpoints.
                    item['status'] = 'checking'
                    self._copy(job, item)
                    item['status'] = 'verified'
                    job['updated_at'] = time.time()
                    _private_json(self._path(job_id), job)
                else:
                    job['status'] = 'completed'
                self._manifest(job)
            except (OSError, ValueError):
                job['status'] = 'failed'
                for item in job['files']:
                    if item['status'] == 'checking':
                        item['status'] = 'failed'
            finally:
                job['updated_at'] = time.time()
                _private_json(self._path(job_id), job)
        return self.report(job_id)

    def _copy(self, job, item):
        # Re-open using the inventory's directory identity; never follow source links.
        location = locate_file(self.inventory, job['inventory_job'], job['source_id'], item['path'])
        parent = str(PurePosixPath(item['path']).parent)
        parent = '' if parent == '.' else parent
        with self.inventory._connect(job['inventory_job']) as db:
            root = db.execute('SELECT path FROM sources WHERE id=?', (job['source_id'],)).fetchone()[0]
            directory = db.execute('SELECT dev,ino FROM dirs WHERE source=? AND path=?',
                                   (job['source_id'], parent)).fetchone()
        source_dir = self.inventory._open_dir(root, parent, tuple(directory))
        try:
            source = os.open(PurePosixPath(location['entry_path']).name,
                             os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=source_dir)
        finally:
            os.close(source_dir)
        target = area = objects = None
        try:
            before = os.fstat(source)
            if not stat.S_ISREG(before.st_mode) or (
                    before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                    item['dev'], item['ino'], item['size'], item['mtime_ns']):
                raise ValueError('source changed')
            digest = _hash(source)
            after = os.fstat(source)
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                    after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise ValueError('source changed during hashing')
            target = _directory(job['destination'])
            identity = os.fstat(target)
            if (identity.st_dev, identity.st_ino) != (job['destination_dev'], job['destination_ino']):
                raise ValueError('destination changed')
            area = _child(target, 'WeChatVault')
            objects = _child(area, 'media')
            existing = None
            try:
                existing = os.open(digest, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                   dir_fd=objects)
                if not stat.S_ISREG(os.fstat(existing).st_mode) or _hash(existing) != digest:
                    raise ValueError('existing archived object is corrupt')
            except FileNotFoundError:
                part = job['job_id'] + '.part'
                # A crashed worker can leave its own unpublished partial file.
                try:
                    os.unlink(part, dir_fd=objects)
                except FileNotFoundError:
                    pass
                out = os.open(part, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                              0o600, dir_fd=objects)
                try:
                    os.lseek(source, 0, os.SEEK_SET)
                    with os.fdopen(os.dup(source), 'rb') as src, os.fdopen(os.dup(out), 'wb') as dst:
                        shutil.copyfileobj(src, dst, 1024 * 1024)
                        dst.flush()
                        os.fsync(dst.fileno())
                    if _hash(out) != digest:
                        raise ValueError('archive checksum mismatch')
                    final = os.fstat(source)
                    if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                            final.st_size, final.st_mtime_ns, final.st_ctime_ns):
                        raise ValueError('source changed during copy')
                    # Atomic publication works on mounted SMB without hardlink support.
                    os.replace(part, digest, src_dir_fd=objects, dst_dir_fd=objects)
                finally:
                    os.close(out)
                    try:
                        os.unlink(part, dir_fd=objects)
                    except FileNotFoundError:
                        pass
            finally:
                if existing is not None:
                    os.close(existing)
            verified = os.open(digest, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                               dir_fd=objects)
            try:
                if not stat.S_ISREG(os.fstat(verified).st_mode) or _hash(verified) != digest:
                    raise ValueError('published destination checksum mismatch')
            finally:
                os.close(verified)
            item['sha256'] = digest
            item['archive_path'] = 'WeChatVault/media/' + digest
        finally:
            os.close(source)
            for fd in (objects, area, target):
                if fd is not None:
                    os.close(fd)

    def _manifest(self, job):
        target = _directory(job['destination'])
        area = manifests = None
        try:
            info = os.fstat(target)
            if (info.st_dev, info.st_ino) != (job['destination_dev'], job['destination_ino']):
                raise ValueError('destination changed')
            area = _child(target, 'WeChatVault')
            manifests = _child(area, 'manifests')
            name = job['job_id'] + '.json'
            temp = job['job_id'] + '.' + str(uuid.uuid4()) + '.tmp'
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=manifests)
            with os.fdopen(fd, 'w') as output:
                json.dump({key: value for key, value in job.items()
                           if key not in ('account_key', 'destination_dev', 'destination_ino')},
                          output, ensure_ascii=False, indent=2)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temp, name, src_dir_fd=manifests, dst_dir_fd=manifests)
        finally:
            for fd in (manifests, area, target):
                if fd is not None:
                    os.close(fd)


if __name__ == '__main__':
    from .storage_inventory import StorageInventory
    ArchiveManager(StorageInventory(Path(sys.argv[1]), sys.argv[2])).run(sys.argv[3])
