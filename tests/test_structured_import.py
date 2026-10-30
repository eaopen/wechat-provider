import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from wechat_provider.cli import import_jsonl
from wechat_provider.service import Service
from wechat_provider.store import Store


class StructuredImportTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.adapter = Mock()
        self.store = Store(self.root / 'index.db')
        self.svc = Service(self.store, self.adapter, 'fixture-a')
        self.path = self.root / 'export.jsonl'

    def row(self, id, day, text=None):
        return dict(chat_id='虚构会话', source_id=id, text=text or '测试消息'+id,
                    sender='虚构联系人', sent_at=f'2026-10-{day:02}T08:00:00+08:00',
                    source='authorized_export')

    def load(self, rows):
        self.path.write_text('\n'.join(json.dumps(r) for r in rows), encoding='utf8')
        return import_jsonl(self.svc, self.path)

    def test_incremental_replay_old_backfill_and_account_isolation(self):
        self.assertEqual(self.load([self.row('3', 3), self.row('2', 2)])['imported'], 2)
        result = self.load([self.row('2', 2), self.row('1', 1)])
        self.assertEqual((result['imported'], result['duplicates']), (1, 1))
        result = self.load([self.row('1', 1), self.row('2', 2)])
        self.assertEqual((result['imported'], result['duplicates']), (0, 2))
        reply = self.svc.get_messages('虚构会话', 2, source='local_index')
        self.assertEqual([m['source_id'] for m in reply['messages']], ['2', '3'])
        self.assertFalse(reply['live_read'])
        self.assertFalse(reply['complete'])
        self.assertEqual(self.store.get_messages('fixture-b', '虚构会话'), [])
        self.assertEqual(self.adapter.mock_calls, [])
        self.assertEqual(len(self.svc.search('测试消息')['matches']), 3)

    def test_conflicting_id_rolls_back_entire_batch_and_fts(self):
        self.load([self.row('1', 1)])
        with self.assertRaisesRegex(ValueError, 'identity conflict'):
            self.load([self.row('2', 2, '回滚测试内容'), self.row('1', 1, 'changed')])
        self.assertEqual(len(self.store.get_messages('fixture-a', '虚构会话')), 1)
        self.assertEqual(self.svc.search('回滚测试内容')['matches'], [])

    def test_malformed_record_is_atomic_and_error_does_not_echo_content(self):
        self.path.write_text(json.dumps(self.row('1', 1))+'\n{"secret-content":', encoding='utf8')
        with self.assertRaisesRegex(ValueError, 'line 2') as caught:
            import_jsonl(self.svc, self.path)
        self.assertNotIn('secret-content', str(caught.exception))
        self.assertEqual(self.store.get_messages('fixture-a', '虚构会话'), [])

    def test_invalid_types_and_ambiguous_times_rejected(self):
        for changes in ({'text': {}}, {'sent_at': '2026-10-01T08:00:00'},
                        {'source_id': 'x\0y'}, {'text': 'x' * 32001}):
            with self.subTest(changes=list(changes)):
                with self.assertRaises(ValueError):
                    self.load([{**self.row('1', 1), **changes}])
        self.assertEqual(self.store.get_messages('fixture-a', '虚构会话'), [])

    def test_timezone_equivalence_is_idempotent(self):
        self.load([self.row('1', 1)])
        result = self.load([{**self.row('1', 1), 'sent_at': '2026-10-01T00:00:00Z'}])
        self.assertEqual(result['duplicates'], 1)

    def test_legacy_import_offset_replays_without_rewrite(self):
        row = self.row('legacy', 1)
        self.store.add_messages('fixture-a', row['chat_id'], [row])
        self.assertEqual(self.load([row])['duplicates'], 1)
