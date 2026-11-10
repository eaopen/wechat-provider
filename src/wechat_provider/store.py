"""SQLite for *our own* index and send ledger; never write WeChat's databases."""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fts = False
        self._initialize()
        if self.path.exists():
            try:
                self.path.chmod(0o600)
            except OSError:
                pass

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(str(self.path), timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def _initialize(self) -> None:
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            migrate_native_accounts = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='native_export_accounts'"
            ).fetchone() is None
            db.executescript("""
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY,
                    account_key TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    text TEXT NOT NULL,
                    sender TEXT,
                    sent_at TEXT,
                    source TEXT NOT NULL,
                    imported_at TEXT NOT NULL,
                    UNIQUE(account_key, chat_id, source_id)
                );
                CREATE INDEX IF NOT EXISTS idx_messages_chat
                    ON messages(account_key, chat_id, sent_at, id);
                CREATE TABLE IF NOT EXISTS imported_conversations (
                    account_key TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    source_namespace TEXT NOT NULL,
                    source_identity TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    PRIMARY KEY(account_key, chat_id)
                );
                CREATE TABLE IF NOT EXISTS import_cursors (
                    account_key TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    cursor TEXT NOT NULL,
                    PRIMARY KEY(account_key, chat_id)
                );
                CREATE TABLE IF NOT EXISTS send_tasks (
                    task_id TEXT PRIMARY KEY,
                    account_key TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    text TEXT NOT NULL,
                    idempotency_key TEXT,
                    status TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(account_key, idempotency_key)
                );
                CREATE TABLE IF NOT EXISTS native_exports (
                    session_id TEXT PRIMARY KEY,
                    fingerprint TEXT NOT NULL UNIQUE,
                    label TEXT NOT NULL,
                    message_count INTEGER NOT NULL,
                    participant_count INTEGER NOT NULL,
                    attachment_count INTEGER NOT NULL,
                    timezone_offset_minutes INTEGER,
                    source_zip_name TEXT,
                    imported_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS native_export_accounts (
                    account_key TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    label TEXT NOT NULL,
                    timezone_offset_minutes INTEGER,
                    source_zip_name TEXT,
                    imported_at TEXT NOT NULL,
                    PRIMARY KEY(account_key, fingerprint)
                );
            """)
            if migrate_native_accounts:
                db.execute("""INSERT OR IGNORE INTO native_export_accounts
                    SELECT DISTINCT m.account_key, n.fingerprint, n.label,
                           n.timezone_offset_minutes, n.source_zip_name, n.imported_at
                    FROM native_exports n JOIN messages m ON m.chat_id=n.session_id
                    WHERE m.source='native_share'""")
            try:
                db.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING "
                    "fts5(text, content='messages', content_rowid='id', tokenize='trigram')"
                )
            except sqlite3.OperationalError:
                try:
                    db.execute(
                        "CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING "
                        "fts5(text, content='messages', content_rowid='id')"
                    )
                except sqlite3.OperationalError:
                    self._fts = False
                else:
                    self._fts = True
            else:
                self._fts = True

            if self._fts:
                db.executescript("""
                    CREATE TRIGGER IF NOT EXISTS msg_ai AFTER INSERT ON messages BEGIN
                      INSERT INTO messages_fts(rowid, text) VALUES (new.id, new.text);
                    END;
                    CREATE TRIGGER IF NOT EXISTS msg_ad AFTER DELETE ON messages BEGIN
                      INSERT INTO messages_fts(messages_fts, rowid, text)
                      VALUES('delete', old.id, old.text);
                    END;
                    CREATE TRIGGER IF NOT EXISTS msg_au AFTER UPDATE ON messages BEGIN
                      INSERT INTO messages_fts(messages_fts, rowid, text)
                      VALUES('delete', old.id, old.text);
                      INSERT INTO messages_fts(rowid, text) VALUES(new.id, new.text);
                    END;
                """)

    def add_messages(self, account_key: str, chat_id: str, rows: list[dict[str, Any]]) -> int:
        """Import only normalized, caller-authorized messages, identified by source_id."""
        count = 0
        with self._connect() as db:
            for row in rows:
                source_id = str(row.get("source_id") or "").strip()
                text = str(row.get("text") or "")
                if not source_id or not text:
                    continue
                result = db.execute(
                    """INSERT OR IGNORE INTO messages
                       (account_key, chat_id, source_id, text, sender, sent_at, source, imported_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (account_key, chat_id, source_id, text,
                     row.get("sender"), row.get("sent_at"),
                     str(row.get("source") or "import"), utc_now()),
                )
                count += result.rowcount
        return count

    def import_messages(self, account_key: str, rows: list[dict[str, Any]],
                        conversations=(), checkpoint=None, expected_checkpoint=None) -> dict:
        """One transaction; duplicate IDs must agree, never silently overwrite history."""
        imported = duplicates = 0
        with self._connect() as db:
            if checkpoint is not None:
                db.execute("BEGIN IMMEDIATE")
                previous = db.execute(
                    "SELECT cursor FROM import_cursors WHERE account_key=? AND chat_id=?",
                    (account_key, checkpoint["chat_id"]),
                ).fetchone()
                actual = json.loads(previous[0]) if previous else None
                if actual != expected_checkpoint:
                    raise ValueError("sync checkpoint changed; retry explicitly")
            for conversation in conversations:
                identity = (account_key, conversation["chat_id"])
                binding = (conversation["source_namespace"], conversation["source_identity"],
                           conversation["conversation_id"], conversation["owner_id"])
                previous = db.execute(
                    "SELECT source_namespace, source_identity, conversation_id, owner_id "
                    "FROM imported_conversations WHERE account_key=? AND chat_id=?", identity,
                ).fetchone()
                if previous and tuple(previous) != binding:
                    raise ValueError("conversation identity conflict; entire import rolled back")
                db.execute(
                    "INSERT INTO imported_conversations VALUES (?,?,?,?,?,?,?) "
                    "ON CONFLICT(account_key,chat_id) DO UPDATE SET title=excluded.title",
                    (*identity, conversation["title"], *binding),
                )
            for row in rows:
                identity = (account_key, row["chat_id"], row["source_id"])
                existing = db.execute(
                    "SELECT text, sender, sent_at, source FROM messages "
                    "WHERE account_key=? AND chat_id=? AND source_id=?", identity,
                ).fetchone()
                values = (row["text"], row.get("sender"), row.get("sent_at"), row["source"])
                if existing is not None:
                    previous = list(existing)
                    # Older imports stored the original ISO offset; compare instants.
                    if previous[2]:
                        try:
                            parsed = datetime.fromisoformat(previous[2].replace("Z", "+00:00"))
                            if parsed.utcoffset() is not None:
                                previous[2] = parsed.astimezone(timezone.utc).isoformat()
                        except ValueError:
                            pass
                    if tuple(previous) != values:
                        raise ValueError("message identity conflict; entire import rolled back")
                    duplicates += 1
                    continue
                db.execute(
                    "INSERT INTO messages(account_key,chat_id,source_id,text,sender,sent_at,"
                    "source,imported_at) VALUES (?,?,?,?,?,?,?,?)",
                    (*identity, *values, utc_now()),
                )
                imported += 1
            if checkpoint is not None:
                db.execute(
                    "INSERT INTO import_cursors VALUES (?,?,?) "
                    "ON CONFLICT(account_key,chat_id) DO UPDATE SET cursor=excluded.cursor",
                    (account_key, checkpoint["chat_id"], json.dumps(checkpoint)),
                )
        return {"imported": imported, "duplicates": duplicates}

    def delete_manual_excerpts(self, account_key: str, chat_title: str) -> int:
        """Delete only explicitly saved copies; preserve all other source records."""
        with self._connect() as db:
            result = db.execute(
                "DELETE FROM messages WHERE account_key=? AND chat_id=? "
                "AND source='manual_excerpt' AND source_id LIKE 'manual:%'",
                (account_key, chat_title),
            )
            return result.rowcount
    def get_import_cursor(self, account_key: str, chat_id: str):
        with self._connect() as db:
            row = db.execute(
                "SELECT cursor FROM import_cursors WHERE account_key=? AND chat_id=?",
                (account_key, chat_id),
            ).fetchone()
        return json.loads(row[0]) if row else None

    def conversation_identity(self, account_key: str, chat_id: str):
        with self._connect() as db:
            row = db.execute(
                "SELECT title, source_namespace, source_identity, conversation_id, owner_id "
                "FROM imported_conversations WHERE account_key=? AND chat_id=?",
                (account_key, chat_id),
            ).fetchone()
        return {**dict(row), "id_kind": "stable_conversation_id"} if row else {
            "id_kind": "display_name",
        }

    def list_chats(self, account_key: str, limit: int = 30) -> list[dict[str, Any]]:
        """One account-scoped index, preserving both imported identities and share provenance."""
        with self._connect() as db:
            rows = db.execute(
                "SELECT m.chat_id, MAX(m.imported_at) AS last_indexed_at, COUNT(*) AS message_count, "
                "COALESCE(c.title,m.chat_id) AS title, c.source_namespace, c.source_identity, "
                "c.conversation_id, c.owner_id, CASE WHEN c.chat_id IS NULL THEN 'display_name' "
                "ELSE 'stable_conversation_id' END AS id_kind, "
                "SUM(CASE WHEN m.source='manual_excerpt' THEN 1 ELSE 0 END) AS manual_count, "
                "SUM(CASE WHEN m.source='native_share' THEN 1 ELSE 0 END) AS native_count "
                "FROM messages m LEFT JOIN imported_conversations c "
                "ON c.account_key=m.account_key AND c.chat_id=m.chat_id "
                "WHERE m.account_key=? GROUP BY m.chat_id ORDER BY MAX(m.id) DESC LIMIT ?",
                (account_key, min(max(1, limit), 100)),
            ).fetchall()
            native_ids = [r['chat_id'] for r in rows if r['native_count'] == r['message_count']]
            labels = {}
            if native_ids:
                marks = ','.join('?' for _ in native_ids)
                labels = dict(db.execute(
                    'SELECT n.session_id, a.label FROM native_exports n '
                    'JOIN native_export_accounts a ON a.fingerprint=n.fingerprint '
                    f'WHERE a.account_key=? AND n.session_id IN ({marks})',
                    [account_key, *native_ids],
                ).fetchall())
        result = []
        for row in rows:
            item = {key:value for key,value in dict(row).items() if value is not None}
            native_count = item.pop('native_count')
            manual_count = item.pop('manual_count')
            native_only = native_count == item['message_count']
            manual_only = manual_count == item['message_count']
            item.update(native_share_only=native_only, manual_excerpt_only=manual_only,
                        source_kind=('native_share' if native_only else 'manual_excerpt' if manual_only
                                     else 'mixed' if native_count or manual_count else 'imported'))
            if native_only:
                item['title'] = labels.get(item['chat_id']) or item['title']
            result.append(item)
        return result

    def record_native_export(self, row: dict[str, Any], account_key: str | None = None) -> bool:
        """Idempotent metadata row in the existing SQLite; never a second index DB."""
        with self._connect() as db:
            result = db.execute(
                """INSERT OR IGNORE INTO native_exports
                   (session_id, fingerprint, label, message_count, participant_count,
                    attachment_count, timezone_offset_minutes, source_zip_name, imported_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (row["session_id"], row["fingerprint"], row["label"], row["message_count"],
                 row["participant_count"], row["attachment_count"],
                 row.get("timezone_offset_minutes"), row.get("source_zip_name"), utc_now()),
            )
            accounts = [account_key] if account_key is not None else [
                existing[0] for existing in db.execute(
                    "SELECT DISTINCT account_key FROM messages WHERE chat_id=? AND source='native_share'",
                    (row["session_id"],),
                )
            ]
            for owner in accounts:
                result = db.execute(
                    "INSERT OR IGNORE INTO native_export_accounts VALUES (?,?,?,?,?,?)",
                    (owner, row["fingerprint"], row["label"],
                     row.get("timezone_offset_minutes"), row.get("source_zip_name"), utc_now()),
                )
            return result.rowcount == 1

    def get_native_export(self, fingerprint: str,
                          account_key: str | None = None) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                """SELECT session_id, fingerprint, label, message_count, participant_count,
                          attachment_count, timezone_offset_minutes, source_zip_name, imported_at
                   FROM native_exports WHERE fingerprint=?""",
                (fingerprint,),
            ).fetchone()
            if row is not None and account_key is not None:
                scoped = db.execute(
                    "SELECT label, timezone_offset_minutes, source_zip_name, imported_at "
                    "FROM native_export_accounts WHERE account_key=? AND fingerprint=?",
                    (account_key, fingerprint),
                ).fetchone()
                return {**dict(row), **dict(scoped)} if scoped else None
        return dict(row) if row else None

    def get_messages(self, account_key: str, chat_id: str, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                """SELECT chat_id, source_id, text, sender, sent_at, source, imported_at
                   FROM messages WHERE account_key=? AND chat_id=?
                   ORDER BY COALESCE(julianday(sent_at), julianday(imported_at)) DESC, id DESC LIMIT ?""",
                (account_key, chat_id, min(max(1, limit), 200)),
            ).fetchall()
        return [dict(x) for x in reversed(rows)]

    def search(self, account_key: str, query: str, chat_id: str | None = None,
               limit: int = 30) -> list[dict[str, Any]]:
        """FTS5 when possible; LIKE fallback preserves CJK and short-term matching."""
        query = query.strip()
        if not query:
            return []
        limit = min(max(1, limit), 100)
        extra = " AND m.chat_id=?" if chat_id else ""
        params: list[Any] = [account_key]
        if chat_id:
            params.append(chat_id)

        with self._connect() as db:
            rows: list[sqlite3.Row] = []
            if self._fts and len(query) >= 3:
                # Always pass query as a literal phrase, never raw FTS syntax.
                phrase = '"' + query.replace('"', '""') + '"'
                try:
                    rows = db.execute(
                        """SELECT m.chat_id, m.source_id, m.text, m.sender, m.sent_at, m.source
                           FROM messages_fts JOIN messages m ON m.id=messages_fts.rowid
                           WHERE messages_fts MATCH ? AND m.account_key=?"""
                        + extra + " ORDER BY m.id DESC LIMIT ?",
                        [phrase] + params + [limit],
                    ).fetchall()
                except sqlite3.OperationalError:
                    rows = []
            if not rows:
                rows = db.execute(
                    """SELECT m.chat_id, m.source_id, m.text, m.sender, m.sent_at, m.source
                       FROM messages m WHERE m.account_key=?"""
                    + extra + " AND m.text LIKE ? ESCAPE '!' ORDER BY m.id DESC LIMIT ?",
                    params + ["%" + query.replace("!", "!!").replace("%", "!%").replace("_", "!_") + "%", limit],
                ).fetchall()
        return [dict(x) for x in rows]

    def queue_send(self, account_key: str, chat_id: str, text: str,
                   idempotency_key: str | None = None) -> dict[str, Any]:
        now = utc_now()
        task_id = str(uuid.uuid4())
        with self._connect() as db:
            db.execute(
                """INSERT OR IGNORE INTO send_tasks
                   (task_id, account_key, chat_id, text, idempotency_key,
                    status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, 'queued', ?, ?)""",
                (task_id, account_key, chat_id, text, idempotency_key, now, now),
            )
            if idempotency_key:
                row = db.execute(
                    "SELECT * FROM send_tasks WHERE account_key=? AND idempotency_key=?",
                    (account_key, idempotency_key),
                ).fetchone()
                if row["chat_id"] != chat_id or row["text"] != text:
                    raise ValueError("Idempotency key already used for different message")
            else:
                row = db.execute(
                    "SELECT * FROM send_tasks WHERE task_id=?", (task_id,)
                ).fetchone()
        return dict(row)

    def claim(self, task_id: str) -> bool:
        with self._connect() as db:
            result = db.execute(
                """UPDATE send_tasks SET status='running', updated_at=?
                   WHERE task_id=? AND status='queued'""",
                (utc_now(), task_id),
            )
        return result.rowcount == 1

    def finish(self, task_id: str, status: str, detail: str = "") -> None:
        if status not in {"submitted", "verified", "uncertain", "failed"}:
            raise ValueError("Unsupported send state")
        with self._connect() as db:
            db.execute(
                """UPDATE send_tasks SET status=?, detail=?, updated_at=?
                   WHERE task_id=? AND status='running'""",
                (status, detail[:500], utc_now(), task_id),
            )

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                """SELECT task_id, account_key, chat_id, status, detail,
                          created_at, updated_at FROM send_tasks WHERE task_id=?""",
                (task_id,),
            ).fetchone()
        return dict(row) if row else None
