import hashlib
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from wechat_provider.adapters.unsupported import UnsupportedAdapter
from wechat_provider.service import Service
from wechat_provider.storage_inventory import StorageInventory
from wechat_provider.store import Store
from wechat_provider.vault_archive import ArchiveManager


class VaultArchiveTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.source = self.home / 'source'
        self.target = self.home / 'target'
        self.source.mkdir()
        self.target.mkdir()
        (self.source / '合同.txt').write_text('synthetic contract')
        self.inventory = StorageInventory(self.home / 'private' / 'inventory', 'fixture-account')
        job = self.inventory.start([{'kind': 'other', 'path': str(self.source)}], launch=False)
        self.inventory.run(job['job_id'])
        self.inventory_job = job['job_id']
        self.archive = ArchiveManager(self.inventory)

    def plan(self):
        return self.archive.plan(self.inventory_job, 1, ['合同.txt'], str(self.target))

    def test_preview_then_copy_manifest_and_source_unchanged(self):
        before = (self.source / '合同.txt').stat()
        job = self.plan()
        self.assertEqual(job['status'], 'planned')
        self.assertEqual(list(self.target.iterdir()), [])
        self.archive.start(job['job_id'], launch=False)
        result = self.archive.run(job['job_id'])
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['verified_files'], 1)
        digest = hashlib.sha256(b'synthetic contract').hexdigest()
        self.assertEqual((self.target / 'WeChatVault/media' / digest).read_bytes(), b'synthetic contract')
        self.assertEqual(self.archive.locate(job['job_id'], 0)['absolute_path'],
                         str(self.target / 'WeChatVault/media' / digest))
        manifest = json.loads((self.target / 'WeChatVault/manifests' / (job['job_id'] + '.json')).read_text())
        self.assertEqual(manifest['files'][0]['path'], '合同.txt')
        self.assertEqual(manifest['source_root'], str(self.source))
        self.assertEqual(manifest['recovery_status'], 'not_verified')
        self.assertEqual((before.st_ino, before.st_mtime_ns),
                         ((self.source / '合同.txt').stat().st_ino,
                          (self.source / '合同.txt').stat().st_mtime_ns))
        second = self.plan()
        self.assertEqual(self.archive.run(second['job_id'])['status'], 'completed')
        self.assertEqual(len(list((self.target / 'WeChatVault/media').iterdir())), 1)

    def test_changed_source_symlinks_and_destination_replacement_fail_closed(self):
        job = self.plan()
        (self.source / '合同.txt').write_text('changed')
        self.assertEqual(self.archive.run(job['job_id'])['status'], 'failed')
        self.assertFalse((self.target / 'WeChatVault').exists())
        (self.source / '合同.txt').unlink()
        (self.source / '合同.txt').symlink_to('/etc/hosts')
        with self.assertRaises(ValueError):
            self.plan()
        self.target.rename(self.home / 'old-target')
        self.target.mkdir()
        with self.assertRaises(ValueError):
            self.archive.start(job['job_id'])

    def test_cancel_resume_corruption_and_crashed_partial(self):
        job = self.plan()
        self.archive.cancel(job['job_id'])
        self.assertEqual(self.archive.run(job['job_id'])['status'], 'cancelled')
        self.archive.start(job['job_id'], launch=False)
        objects = self.target / 'WeChatVault/media'
        objects.mkdir(parents=True)
        (objects / (job['job_id'] + '.part')).write_bytes(b'crashed')
        result = self.archive.run(job['job_id'])
        self.assertEqual(result['status'], 'completed')
        archived = objects / result['files'][0]['sha256']
        archived.write_bytes(b'corrupted destination')
        with self.assertRaises(ValueError):
            self.archive.locate(job['job_id'], 0)
        another = self.plan()
        self.assertEqual(self.archive.run(another['job_id'])['status'], 'failed')
        self.assertEqual(archived.read_bytes(), b'corrupted destination')

    def test_offline_preview_refuses_start_without_creating_a_local_fallback(self):
        job = self.plan()
        before = self.archive._path(job['job_id']).read_bytes()
        self.target.rename(self.home / 'disconnected-target')
        self.assertFalse(self.archive.report(job['job_id'])['destination_online'])
        with self.assertRaisesRegex(ValueError, 'offline or replaced'):
            self.archive.start(job['job_id'])
        self.assertEqual(self.archive._path(job['job_id']).read_bytes(), before)
        self.assertFalse(self.target.exists())
        self.assertEqual((self.source / '合同.txt').read_text(), 'synthetic contract')

    def test_completed_copy_is_historical_when_target_is_offline_or_replaced(self):
        job = self.plan()
        done = self.archive.run(job['job_id'])
        before = self.archive._path(job['job_id']).read_bytes()
        original = self.home / 'disconnected-target'
        self.target.rename(original)
        for replacement in (False, True):
            if replacement:
                self.target.mkdir()
            report = self.archive.report(job['job_id'])
            self.assertEqual(report['status'], 'completed')
            self.assertEqual(report['verified_files'], done['verified_files'])
            self.assertFalse(report['destination_online'])
            with self.assertRaises(ValueError):
                self.archive.locate(job['job_id'], 0)
            with self.assertRaises(ValueError):
                self.archive.start(job['job_id'])
            self.assertEqual(self.archive._path(job['job_id']).read_bytes(), before)
        self.assertEqual(list(self.target.iterdir()), [])
        self.target.rmdir()
        original.rename(self.target)
        self.assertTrue(self.archive.report(job['job_id'])['destination_online'])
        self.assertTrue(self.archive.locate(job['job_id'], 0)['identity_checked'])

    def test_source_mutation_during_copy_does_not_publish(self):
        job = self.plan()
        from wechat_provider import vault_archive
        copy = vault_archive.shutil.copyfileobj
        def mutate(src, dst, length):
            copy(src, dst, length)
            (self.source / '合同.txt').write_text('modified during copy')
        with patch.object(vault_archive.shutil, 'copyfileobj', side_effect=mutate):
            result = self.archive.run(job['job_id'])
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(list((self.target / 'WeChatVault/media').iterdir()), [])

    def test_archive_area_symlink_and_account_isolation(self):
        (self.target / 'WeChatVault').symlink_to(self.source, target_is_directory=True)
        job = self.plan()
        self.assertEqual(self.archive.run(job['job_id'])['status'], 'failed')
        self.assertEqual(len(list(self.source.iterdir())), 1)
        other = ArchiveManager(StorageInventory(self.inventory.directory, 'other-account'))
        with self.assertRaises(ValueError):
            other.report(job['job_id'])
        for destination in (self.source, self.source / 'nested', self.inventory.directory):
            destination.mkdir(exist_ok=True)
            with self.assertRaises(ValueError):
                self.archive.plan(self.inventory_job, 1, ['合同.txt'], str(destination))

    def test_actual_background_worker(self):
        job = self.plan()
        self.archive.start(job['job_id'])
        for _ in range(100):
            result = self.archive.report(job['job_id'])
            if result['status'] in ('completed', 'failed'):
                break
            time.sleep(.03)
        self.assertEqual(result['status'], 'completed')

    def test_excerpt_index_short_chinese_and_no_automation(self):
        service = Service(Store(self.home / 'index.db'), UnsupportedAdapter('fixture'), 'fixture-account')
        service.store.add_messages('fixture-account', 'test', [{'source_id':'id1', 'text':'合同记录'}])
        match = service.search('合同')['matches'][0]
        self.assertEqual(match['source_id'], 'id1')
        result = service.vault('save_excerpt', messages=[match], source_identity=None)
        self.assertEqual(result['saved_messages'], 1)
        self.assertIn('合同记录', Path(result['markdown_path']).read_text())
        self.assertEqual(service.vault('excerpt_locate', job_id=result['excerpt_id'])['absolute_path'],
                         result['markdown_path'])
        self.assertFalse(result['attachments_archived'])
        overview = service.vault('overview')
        self.assertEqual(len(overview['excerpts']), 1)
        other = Service(service.store, UnsupportedAdapter('fixture'), 'other-account')
        self.assertEqual(other.vault('overview')['excerpts'], [])
        for rows in ([], [{'text': ''}], [{'text': 'x' * 200000}]):
            with self.assertRaises(ValueError):
                service.vault('save_excerpt', messages=rows)

    def test_index_page_retains_namespace_and_is_idempotent(self):
        class Reader:
            def query(self, operation, limit, chat_id, cursor):
                return {'source_namespace': 'fixture-reader', 'source_identity': 'fixture-source',
                        'messages': [{'source_id': 'same-id', 'text': '合同片段',
                                      'source': 'structured_db', 'sender': 'Example',
                                      'sent_at': '2026-11-09T00:00:00+00:00'},
                                     {'source_id': 'binary-id', 'text': None}],
                        'has_more': True, 'next_cursor': 'page-2'}
        service = Service(Store(self.home / 'index.db'), UnsupportedAdapter('fixture'),
                          'fixture-account', Reader())
        result = service.vault('index_page', chat_id='fixture-chat', cursor=None, title='Example')
        self.assertEqual(result['imported'], 1)
        self.assertEqual(result['skipped_non_text_or_truncated'], 1)
        self.assertFalse(result['coverage_complete'])
        self.assertEqual(service.vault('index_page', chat_id='fixture-chat')['duplicates'], 1)
        match = service.search('合同')['matches'][0]
        self.assertEqual(match['source_namespace'], 'fixture-reader')
        self.assertEqual(match['conversation_id'], 'fixture-chat')
        self.assertEqual(match['source_id'], 'same-id')

    def test_ipc_clients_share_scan_and_index(self):
        import socket
        import threading

        from wechat_provider.local_ipc import handle, receive, send
        service = Service(Store(self.home / 'index.db'), UnsupportedAdapter('fixture'), 'fixture-account')
        def rpc(method, **kwargs):
            a, b = socket.socketpair()
            def run():
                with a:
                    handle(a, service)
            thread = threading.Thread(target=run)
            thread.start()
            with b:
                send(b, {'version': 1, 'method': method, 'kwargs': kwargs})
                response = receive(b)
            thread.join(2)
            self.assertTrue(response['ok'])
            return response['result']
        rows = [{'chat_id': 'fixture-chat', 'source_id': 'fixture-message', 'text': '合同约定'}]
        export = self.home / 'fixture.jsonl'
        export.write_text('\n'.join(json.dumps(row) for row in rows))
        self.assertEqual(rpc('vault', action='import_jsonl', path=str(export))['imported'], 1)
        self.assertEqual(len(rpc('search', query='合同', source='local_index')['matches']), 1)
        # No second worker is launched while an identical scan remains active.
        with (patch.dict(os.environ, {'WECHAT_PROVIDER_STORAGE_DIR': str(self.inventory.directory)}),
              patch.object(StorageInventory, '_launch')):
            selected = [{'path': str(self.source), 'kind': 'other'}]
            first = rpc('storage_inventory', action='start', sources=selected)
            second = rpc('storage_inventory', action='start', sources=selected)
            self.assertEqual(first['job_id'], second['job_id'])


if __name__ == '__main__':
    unittest.main()
