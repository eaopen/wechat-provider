"""Compatibility between durable-share provenance and structured Vault imports."""
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock

from wechat_provider.adapters.unsupported import UnsupportedAdapter
from wechat_provider.native_export import PLATFORM_SUPPORTED, mint_grant
from wechat_provider.service import Service
from wechat_provider.store import Store


class SharedCatalogTest(unittest.TestCase):
    def test_native_labels_preserve_structured_identities_and_account_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'index.db')
            store.import_messages('owner', [{'chat_id': 'native:example', 'source_id': 'share-1',
                                            'text': 'synthetic share', 'source': 'native_share'}])
            store.record_native_export({'session_id': 'native:example', 'fingerprint': 'a'*64,
                                        'label': 'Synthetic share', 'message_count': 1,
                                        'participant_count': 1, 'attachment_count': 0})
            identity = {'chat_id': 'structured:example', 'title': 'Synthetic stable conversation',
                        'source_namespace': 'synthetic_reader', 'source_identity': 'fixture',
                        'conversation_id': 'stable-example', 'owner_id': 'owner'}
            store.import_messages('owner', [{'chat_id': identity['chat_id'], 'source_id': 'reader-1',
                                            'text': 'synthetic structured', 'source': 'synthetic_reader'}],
                                  [identity])
            chats = {row['chat_id']: row for row in store.list_chats('owner')}
            self.assertEqual(len(chats), 2)
            self.assertEqual(chats['native:example']['title'], 'Synthetic share')
            self.assertTrue(chats['native:example']['native_share_only'])
            self.assertEqual(chats['native:example']['id_kind'], 'display_name')
            self.assertEqual(chats[identity['chat_id']]['id_kind'], 'stable_conversation_id')
            self.assertEqual(chats[identity['chat_id']]['source_identity'], 'fixture')
            self.assertEqual(chats[identity['chat_id']]['title'], identity['title'])
            self.assertFalse(chats[identity['chat_id']]['native_share_only'])
            self.assertEqual(store.list_chats('another-owner'), [])
            self.assertEqual(store.search('another-owner', 'synthetic'), [])


@unittest.skipUnless(PLATFORM_SUPPORTED, 'native-share grants require verified POSIX')
class UnifiedImportTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.store = Store(self.root / 'provider' / 'index.db')
        self.reader = Mock()
        self.reader.status.return_value = {'available': False}
        self.reader.query.return_value = {
            'source_namespace': 'synthetic_reader', 'source_identity': 'fixture-source',
            'messages': [{'source_id': 'fixture-message', 'text': 'synthetic Vault text',
                          'sender': 'Fixture', 'sent_at': '2026-09-05T00:00:00+00:00'}],
            'has_more': False, 'next_cursor': None,
        }
        self.service = Service(self.store, UnsupportedAdapter('fixture'), 'owner', self.reader)
        self.archive = self.root / 'Synthetic share.zip'
        with zipfile.ZipFile(self.archive, 'w') as archive:
            archive.writestr('聊天记录.txt', '·Fixture\n2026年9月5日 08:05\nsynthetic share text\n\n')

    def preview(self, service, archive=None):
        grant = mint_grant(service.grant_dir, archive or self.archive, 'preview')
        return service.native_export_preview(grant['grant_token'])['export']

    def save(self, service, fingerprint, offset, archive=None):
        grant = mint_grant(service.grant_dir, archive or self.archive, 'save')
        return service.native_export_save(grant['grant_token'], fingerprint, offset)

    def test_vault_and_native_share_use_one_account_scoped_index(self):
        status = self.service.status()
        self.assertEqual(status['vault_contract_version'], 1)
        self.assertIn('storage_governance', status)
        self.assertTrue(status['capabilities']['native_export_import'])
        indexed = self.service.vault('index_page', chat_id='fixture-chat', title='Vault fixture')
        self.assertEqual(indexed['imported'], 1)
        self.assertEqual(self.service.vault('index_page', chat_id='fixture-chat')['duplicates'], 1)
        preview = self.preview(self.service)
        saved = self.save(self.service, preview['fingerprint'], 480)
        self.assertEqual(saved['imported'], 1)
        self.assertEqual(self.save(self.service, preview['fingerprint'], -300)['timezone'],
                         {'applied': True, 'offset_minutes': 480})
        chats = {row['chat_id']: row for row in self.service.list_chats(source='local_index')['chats']}
        self.assertEqual(set(chats), {indexed['indexed_chat_id'], saved['session_id']})
        self.assertEqual(chats[saved['session_id']]['title'], preview['label'])
        self.assertTrue(chats[saved['session_id']]['native_share_only'])
        self.assertEqual(chats[indexed['indexed_chat_id']]['id_kind'], 'stable_conversation_id')
        history = self.service.get_messages(saved['session_id'], source='local_index')
        self.assertEqual(history['messages'][0]['sent_at'], '2026-09-05T08:05:00+08:00')
        matches = self.service.search('synthetic')['matches']
        self.assertEqual({row['chat_id'] for row in matches}, set(chats))
        self.assertTrue(all('source_id' in row for row in matches))
        with self.assertRaisesRegex(ValueError, 'read-only'):
            self.service.send_message(indexed['indexed_chat_id'], 'must not send')
        other = Service(self.store, UnsupportedAdapter('fixture'), 'another-owner')
        self.assertEqual(other.list_chats(source='local_index')['chats'], [])
        self.assertEqual(other.search('synthetic')['matches'], [])
        self.assertEqual(other.get_messages(saved['session_id'], source='local_index')['messages'], [])
        self.assertEqual(other.store.conversation_identity('another-owner', indexed['indexed_chat_id']),
                         {'id_kind': 'display_name'})

    def test_same_export_has_independent_account_receipts_labels_and_offsets(self):
        first = self.preview(self.service)
        self.save(self.service, first['fingerprint'], 480)
        other = Service(self.store, UnsupportedAdapter('fixture'), 'another-owner')
        renamed = self.root / 'Another synthetic label.zip'
        renamed.write_bytes(self.archive.read_bytes())
        second = self.preview(other, renamed)
        self.assertEqual(first['fingerprint'], second['fingerprint'])
        self.assertFalse(second['already_saved'])
        saved = self.save(other, second['fingerprint'], -300, renamed)
        self.assertEqual(saved['imported'], 1)
        self.assertFalse(saved['already_imported'])
        self.assertEqual(saved['timezone'], {'applied': True, 'offset_minutes': -300})
        for service, preview, offset in ((self.service, first, 480), (other, second, -300)):
            self.assertTrue(self.preview(service)['already_saved'])
            self.assertEqual(service.list_chats(source='local_index')['chats'][0]['title'],
                             preview['label'])
            receipt = self.save(service, preview['fingerprint'], None)
            self.assertTrue(receipt['already_imported'])
            self.assertEqual(receipt['timezone'], {'applied': True, 'offset_minutes': offset})
            metadata = self.store.get_native_export(preview['fingerprint'], service.account_key)
            self.assertEqual(metadata['label'], preview['label'])
        self.assertIsNone(self.store.get_native_export(first['fingerprint'], 'unrelated-owner'))

    def test_dev_database_migrates_without_rewriting_history_or_send_ledger(self):
        preview = self.preview(self.service)
        saved = self.save(self.service, preview['fingerprint'], 480)
        task = self.store.queue_send('owner', 'synthetic target', 'synthetic task', 'fixture-task')
        original_task = self.store.get_task(task['task_id'])
        with self.store._connect() as database:
            original = tuple(database.execute('SELECT * FROM native_exports').fetchone())
            for table in ('native_export_accounts', 'imported_conversations', 'import_cursors'):
                database.execute(f'DROP TABLE {table}')
        migrated = Store(self.store.path)
        metadata = migrated.get_native_export(preview['fingerprint'], 'owner')
        self.assertEqual(metadata['timezone_offset_minutes'], 480)
        self.assertIsNone(migrated.get_native_export(preview['fingerprint'], 'another-owner'))
        self.assertEqual(migrated.get_task(task['task_id']), original_task)
        with migrated._connect() as database:
            self.assertEqual(tuple(database.execute('SELECT * FROM native_exports').fetchone()),
                             original)
        self.service.store = migrated
        replay = self.save(self.service, preview['fingerprint'], 0)
        self.assertTrue(replay['already_imported'])
        self.assertEqual(replay['timezone'], {'applied': True, 'offset_minutes': 480})
        self.assertEqual(len(migrated.get_messages('owner', saved['session_id'])), 1)
        self.assertEqual(self.service.vault('index_page', chat_id='fixture-chat')['imported'], 1)
