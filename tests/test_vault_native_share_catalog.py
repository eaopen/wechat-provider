"""Compatibility between durable-share provenance and structured Vault imports."""
import tempfile
import unittest
from pathlib import Path

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
