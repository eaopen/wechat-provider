"""Optional fixed-profile read-only CLI. Never discovers accounts or acquires keys."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import threading

from .models import AdapterUnavailable

MAX_OUTPUT = 8 * 1024 * 1024


def bounded(value, maximum=4096):
    return isinstance(value, str) and 0 < len(value.encode('utf-8')) <= maximum and '\0' not in value


class GreenBubblesReader:
    backend = "greenbubbles"
    schema = "greenbubbles.query.v1"
    def __init__(self, executable='', profile='', source_identity='', timeout=15):
        self.executable = executable
        self.profile = profile
        self.source_identity = source_identity
        self.timeout = timeout

    @classmethod
    def from_env(cls):
        return cls(os.environ.get('WECHAT_PROVIDER_GB_EXECUTABLE', ''),
                   os.environ.get('WECHAT_PROVIDER_GB_PROFILE', ''),
                   os.environ.get('WECHAT_PROVIDER_GB_SOURCE_ID', ''))

    def status(self):
        installed = bool(self.executable and Path(self.executable).is_absolute()
                         and Path(self.executable).is_file() and os.access(self.executable, os.X_OK))
        configured = bounded(self.profile, 200) and bounded(self.source_identity, 512)
        return {'backend': self.backend, 'installed': installed, 'configured': configured,
                'available': installed and configured, 'verified': False,
                'id_kind': 'stable_conversation_id', 'source_identity': self.source_identity if configured else None}

    def _run(self, args, query=None):
        # Drain only a bounded stdout payload; never return stderr or command paths.
        try:
            with subprocess.Popen([self.executable, *args], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as proc:
                output = []
                def collect():
                    output.append(proc.stdout.read(MAX_OUTPUT + 1))
                thread = threading.Thread(target=collect, daemon=True)
                thread.start()
                def feed():
                    try:
                        proc.stdin.write((query or '').encode('utf-8'))
                        proc.stdin.close()
                    except (BrokenPipeError, OSError):
                        pass
                writer = threading.Thread(target=feed, daemon=True)
                writer.start()
                thread.join(self.timeout)
                if thread.is_alive() or not output or len(output[0]) > MAX_OUTPUT:
                    proc.kill()
                    thread.join(1)
                    writer.join(1)
                    raise AdapterUnavailable('structured reader timed out or exceeded output limit')
                try:
                    code = proc.wait(timeout=self.timeout)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    raise AdapterUnavailable('structured reader timed out') from None
                if code:
                    if output[0].strip() == b'{"error":"source_access_denied"}':
                        raise AdapterUnavailable('source_access_denied')
                    raise AdapterUnavailable('structured reader query failed; no fallback attempted')
                return json.loads(output[0])
        except (OSError, ValueError, RecursionError):
            raise AdapterUnavailable('structured reader unavailable or invalid response') from None

    def query(self, operation, limit=30, chat_id=None, cursor=None, query=None):
        if not self.status()['available']:
            raise AdapterUnavailable('structured reader not configured; no initialization attempted')
        if operation not in ('conversations.list', 'messages.list', 'messages.search'):
            raise ValueError('unsupported read operation')
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('limit must be 1-100')
        if chat_id is not None and not bounded(chat_id, 200):
            raise ValueError('invalid stable conversation ID')
        if operation == 'messages.list' and not chat_id:
            raise ValueError('stable conversation ID required')
        if cursor is not None and not bounded(cursor):
            raise ValueError('invalid cursor')
        if operation == 'messages.search' and not bounded(query, 16000):
            raise ValueError('invalid search query')
        args = operation.split('.') + ['--profile', self.profile, '--limit', str(limit)]
        if operation.startswith('messages.'):
            args += ['--json']
        if chat_id:
            args += ['--conversation', chat_id]
        if cursor:
            args += ['--cursor', cursor]
        if operation == 'messages.search':
            args += ['--query-stdin']
        data = self._run(args, query)
        try:
            if (data['schema'] != self.schema or type(data['formatVersion']) is not int
                    or data['formatVersion'] != 1 or data['ok'] is not True
                    or data['operation'] != operation or data['source']['identity'] != self.source_identity
                    or data['source']['mode'] not in ('liveEncrypted', 'snapshotEncrypted', 'decrypted')):
                raise ValueError()
            items, page, consistency = data['items'], data['page'], data['consistency']
            if (not isinstance(items, list) or len(items) > limit or type(page['returned']) is not int
                    or page['returned'] != len(items) or type(page['hasMore']) is not bool
                    or type(consistency['coverageComplete']) is not bool
                    or type(consistency['crossDatabaseAtomic']) is not bool
                    or not isinstance(data['warnings'], list)):
                raise ValueError()
            next_cursor = page.get('nextCursor')
            if page['hasMore'] and not bounded(next_cursor):
                raise ValueError()
            rows = []
            seen = set()
            for item in items:
                if not bounded(item['id']) or item['id'] in seen:
                    raise ValueError()
                seen.add(item['id'])
                if operation == 'conversations.list':
                    if not bounded(item['id'], 200) or (item.get('displayName') is not None and not bounded(item['displayName'], 2000)):
                        raise ValueError()
                    rows.append({'chat_id': item['id'], 'conversation_id': item['id'],
                                 'title': item.get('displayName') or item['id'],
                                 'id_kind': 'stable_conversation_id'})
                    continue
                conversation = item['conversationId']
                if not bounded(conversation, 200) or (chat_id and conversation != chat_id):
                    raise ValueError()
                stamp = item['createdAtUnix']
                if type(stamp) is not int:
                    raise ValueError()
                content = item.get('content')
                text = item.get('snippet') if operation == 'messages.search' else (
                    content.get('Text') if isinstance(content, dict) else None)
                if text is not None and (not isinstance(text, str) or '\0' in text or len(text.encode('utf8')) > 32000):
                    raise ValueError()
                sender_id = item['sender']
                if not isinstance(sender_id, str) or len(sender_id.encode('utf8')) > 4096 or (item.get('senderDisplayName') is not None and not bounded(item['senderDisplayName'], 2000)):
                    raise ValueError()
                rows.append({'chat_id': conversation, 'conversation_id': conversation,
                             'message_id': item['id'], 'source_id': item['id'],
                             'text': text, 'content_kind': 'text' if text is not None else 'non_text',
                             'sender': item.get('senderDisplayName') or sender_id, 'sender_id': sender_id,
                             'sent_at': datetime.fromtimestamp(stamp, timezone.utc).isoformat(),
                             'source': 'structured_db',
                             'truncated': item.get('contentTruncated', item.get('snippetTruncated', False))})
            # Upstream pages newest first; expose reading order oldest first, cursor unchanged.
            if operation == 'messages.list':
                rows.reverse()
            key = {'conversations.list': 'chats', 'messages.list': 'messages', 'messages.search': 'matches'}[operation]
            return {key: rows, 'chat_id': chat_id, 'source': 'structured_db',
                    'id_kind': 'stable_conversation_id', 'source_identity': self.source_identity,
                    'source_namespace': self.backend, 'source_mode': data['source']['mode'],
                    'live_read': False, 'complete': False,
                    'coverage_complete': consistency['coverageComplete'], 'consistency': consistency,
                    'warnings': data['warnings'], 'has_more': page['hasMore'], 'next_cursor': next_cursor}
        except (KeyError, TypeError, ValueError, OverflowError, OSError, AttributeError):
            raise AdapterUnavailable('structured reader response rejected; identity/schema/range mismatch') from None
