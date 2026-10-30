"""SQLite for *our own* index and send ledger; never write WeChat's databases."""

from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


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
            """)
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

    def list_chats(self, account_key: str, limit: int = 30) -> list[dict[str, Any]]:
        """Only our existing index; never enumerate a desktop or another account."""
        with self._connect() as db:
            rows = db.execute(
                "SELECT chat_id, MAX(imported_at) AS last_indexed_at, COUNT(*) AS message_count "
                "FROM messages WHERE account_key=? GROUP BY chat_id ORDER BY MAX(id) DESC LIMIT ?",
                (account_key, min(max(1, limit), 100)),
            ).fetchall()
        return [{**dict(row), "title": row["chat_id"], "id_kind": "display_name"} for row in rows]

    def get_messages(self, account_key: str, chat_id: str, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                """SELECT chat_id, source_id, text, sender, sent_at, source, imported_at
                   FROM messages WHERE account_key=? AND chat_id=?
                   ORDER BY id DESC LIMIT ?""",
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
                        """SELECT m.chat_id, m.text, m.sender, m.sent_at, m.source
                           FROM messages_fts JOIN messages m ON m.id=messages_fts.rowid
                           WHERE messages_fts MATCH ? AND m.account_key=?"""
                        + extra + " ORDER BY m.id DESC LIMIT ?",
                        [phrase] + params + [limit],
                    ).fetchall()
                except sqlite3.OperationalError:
                    rows = []
            if not rows:
                rows = db.execute(
                    """SELECT m.chat_id, m.text, m.sender, m.sent_at, m.source
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
