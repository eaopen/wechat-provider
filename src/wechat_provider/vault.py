"""Local desktop use cases for selected history and verified file archives."""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from pathlib import Path

from .storage_inventory import StorageInventory


def invoke(service, action, **kwargs):
    excerpt_dir = service.store.path.parent / 'vault-excerpts' / hashlib.sha256(
        service.account_key.encode()).hexdigest()[:16]
    if action == 'excerpt_locate':
        excerpt_id = kwargs['job_id']
        if not isinstance(excerpt_id, str) or str(uuid.UUID(excerpt_id)) != excerpt_id:
            raise ValueError('invalid excerpt ID')
        path = excerpt_dir / (excerpt_id + '.md')
        if not path.is_file() or path.is_symlink() or excerpt_dir.is_symlink():
            raise ValueError('excerpt unavailable')
        return {'vault_version': 1, 'job_id': excerpt_id, 'identity_checked': True,
                'source_mutations': False, 'absolute_path': str(path)}
    if action == 'overview':
        manager = StorageInventory(
            Path(os.environ.get('WECHAT_PROVIDER_STORAGE_DIR', str(service.store.path.parent / 'storage'))),
            service.account_key,
        )
        latest = None
        if manager.directory.is_dir():
            for folder in sorted(manager.directory.iterdir(), key=lambda p: p.stat().st_mtime,
                                 reverse=True)[:100]:
                try:
                    report = manager.report(folder.name)
                    if report['status'] == 'completed':
                        latest = report['job_id']
                        break
                except (OSError, ValueError):
                    continue
        excerpts = []
        if excerpt_dir.is_dir() and not excerpt_dir.is_symlink():
            for path in sorted(excerpt_dir.glob('*.json'), key=lambda p: p.stat().st_mtime,
                               reverse=True)[:50]:
                if path.is_symlink():
                    continue
                data = json.loads(path.read_text())
                excerpts.append({'excerpt_id': data['excerpt_id'],
                                 'message_count': len(data['messages']),
                                 'created_at': path.stat().st_mtime,
                                 'markdown_path': str(path.with_suffix('.md'))})
        return {'vault_version': 1, 'latest_inventory': latest, 'excerpts': excerpts}
    if action == 'index_page':
        chat_id = kwargs['chat_id']
        page = service.get_messages(chat_id, limit=50, source='structured_db',
                                    cursor=kwargs.get('cursor'))
        identity = [page['source_namespace'], page['source_identity'], chat_id]
        indexed_id = 'vault:' + hashlib.sha256(json.dumps(identity).encode()).hexdigest()
        conversation = {'chat_id': indexed_id, 'title': kwargs.get('title') or chat_id,
                        'source_namespace': page['source_namespace'],
                        'source_identity': page['source_identity'],
                        'conversation_id': chat_id, 'owner_id': service.account_key}
        if not isinstance(conversation['title'], str) or len(conversation['title'].encode()) > 2000:
            raise ValueError('invalid conversation title')
        rows = [{**row, 'chat_id': indexed_id,
                 'source': page['source_namespace']} for row in page['messages']
                if row.get('text') and not row.get('truncated')]
        result = service.store.import_messages(service.account_key, rows, [conversation])
        return {'vault_version': 1, **result, 'indexed_chat_id': indexed_id,
                'skipped_non_text_or_truncated': len(page['messages']) - len(rows),
                'has_more': page['has_more'], 'next_cursor': page['next_cursor'],
                'coverage_complete': False, 'source_mutations': False}
    if action == 'import_jsonl':
        from .cli import import_jsonl
        return {'vault_version': 1, **import_jsonl(service, Path(kwargs['path'])),
                'coverage_complete': False, 'source_mutations': False}
    if action == 'save_excerpt':
        rows = kwargs.get('messages')
        if (not isinstance(rows, list) or not 1 <= len(rows) <= 50
                or len(json.dumps(rows, ensure_ascii=False).encode()) > 128000):
            raise ValueError('select 1-50 bounded messages')
        clean = []
        for row in rows:
            if (not isinstance(row, dict) or not isinstance(row.get('text'), str)
                    or not row['text'] or '\0' in row['text']):
                raise ValueError('text excerpts required')
            selected = {key: row.get(key) for key in (
                'chat_id', 'source_id', 'message_id', 'text', 'sender', 'sent_at', 'source')}
            if any(value is not None and not isinstance(value, str) for value in selected.values()):
                raise ValueError('invalid excerpt metadata')
            clean.append(selected)
        folder = excerpt_dir
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        if any(p.is_symlink() for p in (folder, folder.parent)) or folder.stat().st_mode & 0o077:
            raise ValueError('excerpt state must be private')
        excerpt_id = str(uuid.uuid4())
        data = {'vault_version': 1, 'excerpt_id': excerpt_id, 'kind': 'user_selected_excerpt',
                'messages': clean, 'source_identity': kwargs.get('source_identity'),
                'coverage_complete': False, 'attachments_archived': False}
        from .vault_archive import _private_json
        _private_json(folder / (excerpt_id + '.json'), data)
        text = '# 微信精选摘录\n\n仅含用户所选文字；附件未归档，不是可恢复备份。\n\n'
        for row in clean:
            text += f"{row.get('sent_at') or '时间未知'} · {row.get('sender') or '发言者未知'}\n\n"
            text += '\n'.join('> ' + line for line in row['text'].splitlines()) + '\n\n'
            text += f"来源消息：{row.get('message_id') or row.get('source_id') or '未提供稳定 ID'}\n\n"
        path = folder / (excerpt_id + '.md')
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as output:
            output.write(text)
        return {'vault_version': 1, 'excerpt_id': excerpt_id, 'saved_messages': len(clean),
                'markdown_path': str(path), 'coverage_complete': False,
                'attachments_archived': False, 'source_mutations': False}
    from .vault_archive import ArchiveManager
    manager = ArchiveManager(StorageInventory(
        Path(os.environ.get('WECHAT_PROVIDER_STORAGE_DIR', str(service.store.path.parent / 'storage'))),
        service.account_key,
    ))
    if action == 'archive_plan':
        return manager.plan(kwargs['inventory_job'], kwargs['source_id'], kwargs['entries'],
                            kwargs['destination'])
    if action in ('archive_report', 'archive_start', 'archive_cancel'):
        return getattr(manager, action.removeprefix('archive_'))(kwargs['job_id'])
    if action == 'archive_locate':
        return manager.locate(kwargs['job_id'], kwargs['index'])
    raise ValueError('unsupported vault action')
