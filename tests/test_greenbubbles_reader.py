from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from wechat_provider.greenbubbles_reader import GreenBubblesReader
from wechat_provider.models import AdapterUnavailable
from wechat_provider.service import Service
from wechat_provider.store import Store


def envelope(operation='messages.list'):
    return {'schema': 'greenbubbles.query.v1', 'formatVersion': 1, 'operation': operation,
            'ok': True, 'source': {'identity': 'synthetic-source', 'mode': 'decrypted'},
            'consistency': {'coverageComplete': False, 'crossDatabaseAtomic': False},
            'page': {'returned': 1, 'hasMore': True, 'nextCursor': 'cursor-1'}, 'warnings': [],
            'items': [{'id': 'message-1', 'conversationId': 'wxid_a', 'sender': 'wxid_b',
                       'senderDisplayName': 'same name', 'createdAtUnix': 100,
                       'content': {'Text': 'synthetic text'}, 'snippet': 'synthetic text'}]}


class ReaderTest(unittest.TestCase):
    def setUp(self):
        self.reader = GreenBubblesReader(sys.executable, 'fixture', 'synthetic-source', timeout=.1)

    def test_status_does_not_execute_and_missing_config_stops(self):
        with patch('subprocess.Popen') as run:
            self.assertTrue(self.reader.status()['available'])
            self.assertFalse(self.reader.status()['verified'])
            with self.assertRaises(AdapterUnavailable):
                GreenBubblesReader().query('messages.list', 1, 'wxid_a')
            run.assert_not_called()

    def test_exact_identity_pagination_non_text_and_search_stdin(self):
        data = envelope()
        data['items'].append({**data['items'][0], 'id': 'message-2', 'content': {'Image': {}}})
        data['page']['returned'] = 2
        with patch.object(self.reader, '_run', return_value=data) as run:
            result = self.reader.query('messages.list', 2, 'wxid_a', 'old-cursor')
            self.assertEqual(result['messages'][0]['content_kind'], 'non_text')
            self.assertEqual(result['messages'][1]['message_id'], 'message-1')
            self.assertEqual(result['next_cursor'], 'cursor-1')
            self.assertFalse(result['complete'])
            self.assertIn('--profile', run.call_args.args[0])
        with patch.object(self.reader, '_run', return_value=envelope('messages.search')) as run:
            self.reader.query('messages.search', 1, query='private search')
            self.assertNotIn('private search', run.call_args.args[0])
            self.assertEqual(run.call_args.args[1], 'private search')

    def test_wrong_schema_source_chat_and_count_rejected(self):
        for field, value in [('schema', 'v2'), ('formatVersion', True), ('ok', False)]:
            data = envelope(); data[field] = value
            with patch.object(self.reader, '_run', return_value=data), self.assertRaises(AdapterUnavailable):
                self.reader.query('messages.list', 1, 'wxid_a')
        for mutate in [lambda d: d['source'].update(identity='other'),
                       lambda d: d['items'][0].update(conversationId='other'),
                       lambda d: d['page'].update(returned=2),
                       lambda d: d['page'].update(nextCursor=None)]:
            data = envelope(); mutate(data)
            with patch.object(self.reader, '_run', return_value=data), self.assertRaises(AdapterUnavailable):
                self.reader.query('messages.list', 1, 'wxid_a')

    def test_same_names_keep_distinct_conversation_ids(self):
        data = envelope('conversations.list')
        data['items'] = [{'id': 'wxid_a', 'displayName': 'same'}, {'id': 'wxid_b', 'displayName': 'same'}]
        data['page']['returned'] = 2
        with patch.object(self.reader, '_run', return_value=data):
            result = self.reader.query('conversations.list', 2)
            self.assertEqual([x['chat_id'] for x in result['chats']], ['wxid_a', 'wxid_b'])

    def test_timeout_malformed_and_output_limit_are_sanitized(self):
        for code in ['import time; time.sleep(2)', 'print("private malformed data")', 'print("x"*100)']:
            with patch('wechat_provider.greenbubbles_reader.MAX_OUTPUT', 40):
                with self.assertRaises(AdapterUnavailable) as error:
                    self.reader._run(['-c', code])
                self.assertNotIn('private', str(error.exception))

    def test_no_gui_no_index_copy_and_old_source_compatibility(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp)/'index.db'); gui = Mock()
            service = Service(store, gui, 'a', self.reader)
            with patch.object(self.reader, '_run', return_value=envelope()):
                result = service.get_messages('wxid_a', 1, 'structured_db')
                self.assertEqual(result['id_kind'], 'stable_conversation_id')
            self.assertEqual(service.get_messages('wxid_a', source='local_index')['messages'], [])
            with patch.object(self.reader, '_run', return_value=envelope('messages.search')):
                self.assertEqual(len(service.search('hello', 'wxid_a', 1, 'structured_db')['matches']), 1)
            self.assertEqual(gui.mock_calls, [])
            with self.assertRaises(ValueError):
                service.get_messages('wxid_a', source='local_index', cursor='x')

    def test_invalid_requests_do_not_execute(self):
        with patch.object(self.reader, '_run') as run:
            for operation, limit, chat in [('send', 1, 'x'), ('messages.list', 0, 'x'),
                                           ('messages.list', 1, None)]:
                with self.assertRaises(ValueError):
                    self.reader.query(operation, limit, chat)
            run.assert_not_called()
