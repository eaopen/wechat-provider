"""Private subprocess using the owner's pinned Wechat-MCP SDK and existing keys only."""
import argparse
import base64
import hashlib
import json
import sys
from pathlib import Path


def encode_cursor(binding, key):
    return base64.urlsafe_b64encode(json.dumps([1, binding, key]).encode()).decode()


def decode_cursor(value, binding):
    if not value:
        return None
    version, actual, key = json.loads(base64.b64decode(value, altchars=b'-_', validate=True))
    if version != 1 or actual != binding:
        raise ValueError('cursor binding mismatch')
    return key


def query(adapter, operation, limit, conversation_id=None, cursor=None, search=None):
    from wechat_bridge.adapters.messages import decode_message
    from wechat_bridge.adapters.models import SourceRef
    from wechat_bridge.core.clock import now_utc
    identity = 'sha256:' + hashlib.sha256(
        (adapter.inventory.account_id + str(adapter.config.state_dir.resolve())).encode()).hexdigest()
    binding = [identity, operation, conversation_id, search]
    before = decode_cursor(cursor, binding)
    items = []
    keys = []
    if operation == 'conversations.list':
        if before is not None and not isinstance(before, str):
            raise ValueError('invalid cursor')
        conversations = sorted(adapter.conversations(), key=lambda c: c.conversation_id)
        for c in conversations:
            if before is None or c.conversation_id > before:
                items.append({'id': c.conversation_id, 'displayName': c.display_name})
                keys.append(c.conversation_id)
                if len(items) > limit:
                    break
    else:
        if not conversation_id:
            raise ValueError('explicit conversation required')
        if before is not None and (not isinstance(before, list) or len(before) != 3 or
                type(before[0]) is not int or not isinstance(before[1], str) or type(before[2]) is not int):
            raise ValueError('invalid cursor')
        c = adapter.conversation(conversation_id)
        candidates = []
        for schema, table in adapter._message_tables(c.source_username):
            adapter.identities.add_conversation(schema.database_id, table, c.source_username)
            where, params = '1', []
            if before is not None:
                ts, dbid, local = before
                if schema.database_id == dbid:
                    where, params = '(create_time<? OR (create_time=? AND local_id<?))', [ts, ts, local]
                elif schema.database_id < dbid:
                    where, params = 'create_time<=?', [ts]
                else:
                    where, params = 'create_time<?', [ts]
            # Search scans a bounded recent window, including compressed text after decoding.
            cap = 100 if operation == 'messages.search' else limit + 1
            columns = ('local_id,server_id,local_type,real_sender_id,create_time,'
                       'CAST(message_content AS BLOB) AS message_content,WCDB_CT_message_content')
            if 'packed_info_data' in dict(schema.table(table).columns):
                columns += ',packed_info_data'
            with adapter._open(schema.relative_path) as reader, reader.transaction() as tx:
                rows = tx.query('SELECT '+columns+' FROM '+schema.quote_table(table)+' WHERE '+where+
                                ' ORDER BY create_time DESC,local_id DESC LIMIT ?', tuple(params+[cap]), max_rows=cap)
                sender_ids = sorted({r['real_sender_id'] for r in rows if type(r['real_sender_id']) is int})
                if sender_ids:
                    for row in tx.query('SELECT rowid,user_name FROM Name2Id WHERE rowid IN ('+
                                        ','.join('?' for _ in sender_ids)+')', tuple(sender_ids), max_rows=cap):
                        adapter.identities.add_sender(schema.database_id, row['rowid'], row['user_name'])
                observed = now_utc()
            for row in rows:
                source = SourceRef(reader.db.account_id, schema.database_id, table,
                                   str(row['local_id']), str(row['server_id']) if row['server_id'] else None, observed)
                message = decode_message(row | {'timestamp_unit': 'seconds'}, source, adapter.identities)
                if message.utc_timestamp is None:
                    continue
                key = [int(message.source_timestamp), schema.database_id, row['local_id']]
                item = {'id': message.message_id, 'conversationId': message.conversation_id,
                        'sender': message.sender_id or '', 'createdAtUnix': key[0],
                        'content': {'Text': message.text} if message.text is not None else {},
                        'contentTruncated': False, 'parse_status': message.parse_status}
                candidates.append((key, item))
        candidates.sort(key=lambda pair: pair[0], reverse=True)
        if operation == 'messages.search':
            # Advance by inspected rows, not just hits: an empty page may have more data.
            inspected = candidates[:100]
            matches = [(k, v) for k, v in inspected if search in (v['content'].get('Text') or '')]
            selected = matches[:limit]
            has_more = len(matches) > limit or len(candidates) >= 100
            next_key = selected[-1][0] if len(matches) > limit else (inspected[-1][0] if inspected else None)
            items = [dict(v, snippet=v['content']['Text'], snippetTruncated=False) for k, v in selected]
            return envelope(identity, operation, items, has_more,
                            encode_cursor(binding, next_key) if has_more and next_key else None)
        keys = [k for k, _ in candidates]
        items = [v for _, v in candidates]
    more = len(items) > limit
    return envelope(identity, operation, items[:limit], more,
                    encode_cursor(binding, keys[limit-1]) if more else None)


def envelope(identity, operation, items, more, cursor):
    from wechat_bridge.core.clock import now_utc
    return {'schema': 'wechat-provider.query.v1', 'formatVersion': 1, 'operation': operation,
            'ok': True, 'source': {'identity': identity, 'mode': 'liveEncrypted'},
            'consistency': {'coverageComplete': False, 'crossDatabaseAtomic': False, 'observedAt': now_utc()},
            'page': {'returned': len(items), 'hasMore': more, 'nextCursor': cursor},
            'warnings': [{'code': 'partial_read', 'message': 'Local saved-key source only; unsupported records and auxiliary storage may be absent.'}],
            'items': items}


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--state-dir', required=True)
    p.add_argument('resource', choices=['conversations', 'messages'])
    p.add_argument('action', choices=['list', 'search'])
    p.add_argument('--profile', choices=['bound'], required=True)
    p.add_argument('--limit', type=int, default=10)
    p.add_argument('--conversation')
    p.add_argument('--cursor')
    p.add_argument('--json', action='store_true')
    p.add_argument('--query-stdin', action='store_true')
    args = p.parse_args()
    try:
        from wechat_bridge.core.config import load_config
        from wechat_bridge.adapters.wechat_4_1 import open_adapter, ADAPTER_VERSION
        if ADAPTER_VERSION != 'wechat-4.1.15-270102-270135-v2' or not 1 <= args.limit <= 100:
            raise ValueError()
        state = Path(args.state_dir)
        if not all((state / f).is_file() for f in ('config.json', 'keys.json', 'identity.json')):
            raise ValueError()
        operation = args.resource+'.'+args.action
        if operation not in ('conversations.list', 'messages.list', 'messages.search'):
            raise ValueError()
        search = sys.stdin.read(16001) if args.query_stdin else None
        if operation == 'messages.search' and (not search or len(search.encode()) > 16000):
            raise ValueError()
        config = load_config(state)
        # pathlib recursive glob may silently hide permission errors. Probe first.
        import os
        with os.scandir(config.account_root / 'db_storage') as entries:
            next(entries, None)
        adapter = open_adapter(config)
        print(json.dumps(query(adapter, operation, args.limit, args.conversation, args.cursor, search)))
    except PermissionError:
        print('{"error":"source_access_denied"}')
        return 1
    except Exception:
        print('{"error":"wechat_mcp_read_failed"}')
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
