import contextlib
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from wechat_provider.wechat_mcp_reader import WechatMCPReader, configured_reader
from wechat_provider.wechat_mcp_worker import encode_cursor, decode_cursor, query


class WorkerTest(unittest.TestCase):
    def test_cursor_binds_account_operation_chat_and_search(self):
        binding = ['source', 'messages.list', 'chat', None]
        cursor = encode_cursor(binding, [5, 'shard', 2])
        self.assertEqual(decode_cursor(cursor, binding), [5, 'shard', 2])
        for changed in [['other', *binding[1:]], ['source', 'messages.search', 'chat', None],
                        ['source', 'messages.list', 'other', None],
                        ['source', 'messages.list', 'chat', 'query']]:
            with self.subTest(binding=changed), self.assertRaises(ValueError):
                decode_cursor(cursor, changed)

    def test_selected_reader_missing_config_does_not_fallback(self):
        with patch.dict(os.environ, {'WECHAT_PROVIDER_READER': 'wechat_mcp'}, clear=True):
            self.assertIsInstance(configured_reader(), WechatMCPReader)
            self.assertFalse(configured_reader().status()['available'])

    def test_saved_state_needed_and_status_does_not_run(self):
        with tempfile.TemporaryDirectory() as d:
            reader = WechatMCPReader(sys.executable, d, 'source')
            self.assertFalse(reader.status()['available'])
            for name in ['config.json', 'keys.json', 'identity.json']:
                (Path(d)/name).touch()
            with patch('subprocess.Popen') as run:
                self.assertTrue(reader.status()['available'])
                self.assertFalse(reader.status()['verified'])
                run.assert_not_called()

    def test_real_worker_merge_pagination_with_synthetic_sdk(self):
        # Exercise SQL keyset conditions, equal timestamps across shards, bounded merge.
        import sqlite3
        dbs = {}
        for shard in ['a', 'b']:
            db = sqlite3.connect(':memory:'); db.row_factory = sqlite3.Row
            db.executescript('CREATE TABLE Msg_test(local_id,server_id,local_type,real_sender_id,create_time,message_content,WCDB_CT_message_content); CREATE TABLE Name2Id(user_name);')
            for i in range(1, 5):
                db.execute('INSERT INTO Msg_test VALUES (?,?,1,1,10,?,0)', (i,i,'synthetic'))
            db.execute("INSERT INTO Name2Id VALUES ('sender')")
            dbs[shard] = db
            self.addCleanup(db.close)
        class Reader:
            def __init__(self, shard): self.shard=shard; self.db=types.SimpleNamespace(account_id='fixture')
            def __enter__(self): return self
            def __exit__(self,*args): pass
            def transaction(self): return contextlib.nullcontext(self)
            def query(self, sql, params=(), max_rows=200):
                return [dict(r) for r in dbs[self.shard].execute(sql, params).fetchall()]
        schemas = [types.SimpleNamespace(database_id=s,relative_path=s,
                    table=lambda _:types.SimpleNamespace(columns=[]),quote_table=lambda _: 'Msg_test') for s in dbs]
        adapter = types.SimpleNamespace(inventory=types.SimpleNamespace(account_id='fixture'),
            config=types.SimpleNamespace(state_dir=Path('/synthetic')),
            conversation=lambda _: types.SimpleNamespace(source_username='fixture'),
            identities=types.SimpleNamespace(add_conversation=lambda *a:None,add_sender=lambda *a:None),
            _message_tables=lambda _: [(s,'Msg_test') for s in schemas],_open=Reader)
        def decode(row, source, identities):
            return types.SimpleNamespace(utc_timestamp='valid',source_timestamp='10',
                message_id=source.database_id+str(source.local_id),conversation_id='chat',sender_id='sender',
                text='synthetic',parse_status='ok')
        modules={
            'wechat_bridge.adapters.messages': types.SimpleNamespace(decode_message=decode),
            'wechat_bridge.adapters.models': types.SimpleNamespace(SourceRef=lambda a,d,t,l,s,o:types.SimpleNamespace(database_id=d,local_id=l)),
            'wechat_bridge.core.clock': types.SimpleNamespace(now_utc=lambda:'now')}
        with patch.dict(sys.modules,modules):
            cursor=None; ids=[]
            for _ in range(5):
                page=query(adapter,'messages.list',2,'chat',cursor)
                ids.extend(x['id'] for x in page['items'])
                if not page['page']['hasMore']: break
                cursor=page['page']['nextCursor']
            self.assertEqual(ids,['b4','b3','b2','b1','a4','a3','a2','a1'])
            result=query(adapter,'messages.search',2,'chat',search='absent')
            self.assertEqual(result['items'],[])
            self.assertFalse(result['page']['hasMore'])
