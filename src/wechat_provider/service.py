"""MCP-facing use cases: no WeChat database writes, no automatic re-send."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

from .adapters import native_adapter
from .locks import desktop_lock
from .models import AdapterUnavailable, WeChatAdapter
from .store import Store
from .greenbubbles_reader import GreenBubblesReader
from .wechat_mcp_reader import configured_reader


def default_db_path() -> Path:
    override = os.environ.get("WECHAT_PROVIDER_DB")
    if override:
        return Path(override).expanduser()
    if sys.platform == "win32":
        home = Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
        return home / "wechat-provider" / "wechat.db"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "wechat-provider" / "wechat.db"
    return Path.home() / ".local" / "share" / "wechat-provider" / "wechat.db"


class Service:
    def __init__(self, store: Store, adapter: WeChatAdapter,
                 account_key: str = "default", reader=None):
        self.reader = reader if reader is not None else GreenBubblesReader()
        self.store = store
        self.adapter = adapter
        self.account_key = account_key
        self.lock_path = store.path.with_name("wechat-rpa.lock")

    def status(self) -> dict[str, Any]:
        adapter = self.adapter.status()
        reader = self.reader.status()
        return {
            "provider": "wechat-provider",
            "account_key": self.account_key,
            "db_path": str(self.store.path),
            "fts5_enabled": self.store._fts,
            "adapter": adapter,
            "bridge_required": False,
            "read_contract_version": 1,
            "readers": {"local_index": {"available": True}, "structured_db": reader},
            "senders": {"native_ui": {"selected": True,
                "available": adapter.get("ready_for_attempt", False), "verified": False}},
            "capabilities": {
                "local_index_read": True,
                "structured_db_read": reader["available"],
                "live_read_available": adapter.get("ready_for_attempt", False),
                "live_read_verified": False,
                "send_verified": False,
                "id_kind": "display_name",
            },
        }

    def list_chats(self, limit: int = 30, source: str = "desktop_ui", cursor: str | None = None) -> dict[str, Any]:
        if source == "structured_db":
            return self.reader.query("conversations.list", limit, cursor=cursor)
        if cursor is not None:
            raise ValueError("cursor requires structured_db")
        limit = min(max(1, limit), 100)
        if source not in {"desktop_ui", "local_index"}:
            raise ValueError("source must be desktop_ui or local_index")
        if source == "local_index":
            return {"chats": self.store.list_chats(self.account_key, limit),
                    "source": "local_index", "complete": False, "live_read": False,
                    "warning": "Previously indexed/imported chats only; not current WeChat sessions."}
        try:
            with desktop_lock(self.lock_path):
                chats = self.adapter.list_chats(limit)
            return {"chats": chats, "source": "desktop_ui", "complete": False}
        except AdapterUnavailable as exc:
            return {"chats": [], "error": str(exc), "complete": False}

    def get_messages(self, chat_id: str, limit: int = 50, source: str = "desktop_ui", cursor: str | None = None) -> dict[str, Any]:
        if source == "structured_db":
            return self.reader.query("messages.list", limit, chat_id, cursor)
        if cursor is not None:
            raise ValueError("cursor requires structured_db")
        chat_id = chat_id.strip()
        if not chat_id or len(chat_id) > 200 or "\0" in chat_id:
            raise ValueError("chat_id must be 1-200 characters")
        if source not in {"desktop_ui", "local_index"}:
            raise ValueError("source must be desktop_ui or local_index")
        if source == "local_index":
            return {"messages": self.store.get_messages(self.account_key, chat_id, limit),
                    "chat_id": chat_id, "source": "local_index", "complete": False,
                    "live_read": False, "id_kind": "display_name",
                    "warning": "Previously indexed/imported text only; not a live WeChat read."}
        limit = min(max(1, limit), 100)
        try:
            with desktop_lock(self.lock_path):
                history = self.adapter.get_messages(chat_id, limit)
        except AdapterUnavailable as exc:
            cache = self.store.get_messages(self.account_key, chat_id, limit)
            return {
                "messages": cache, "source": "local_index",
                "complete": False, "warning": str(exc), "live_read": False,
                "read_status": "cached_fallback", "chat_id": chat_id, "id_kind": "display_name",
            }

        normalized = []
        for index, item in enumerate(history[-limit:]):
            content = str(item.get("text") or "")
            if not content:
                continue
            # UI provides no guaranteed stable IDs; hashing is only best-effort.
            source_id = str(item.get("source_id") or "")
            if not source_id:
                stable = json.dumps(
                    [chat_id, index, content, item.get("sender"), item.get("sent_at")],
                    ensure_ascii=False, sort_keys=True,
                )
                source_id = "ui:" + hashlib.sha256(stable.encode()).hexdigest()
            normalized.append({
                "source_id": source_id,
                "text": content,
                "sender": item.get("sender"),
                "sent_at": item.get("sent_at"),
                "source": item.get("source") or "desktop_ui",
            })
        self.store.add_messages(self.account_key, chat_id, normalized)
        return {
            "messages": normalized,
            "source": "desktop_ui",
            "chat_id": chat_id, "id_kind": "display_name", "live_read": True,
            "complete": False,
            "warning": "UI view only. Names/IDs and history completeness are not guaranteed.",
        }

    def search(self, query: str, chat_id: str | None = None,
               limit: int = 30, source: str = "local_index", cursor: str | None = None) -> dict[str, Any]:
        if source == "structured_db":
            return self.reader.query("messages.search", limit, chat_id, cursor, query)
        if source != "local_index" or cursor is not None:
            raise ValueError("unsupported search source or cursor")
        rows = self.store.search(self.account_key, query, chat_id, limit)
        return {
            "matches": rows,
            "source": "local_index",
            "complete": False,
            "warning": "Search covers only messages previously read/imported; not all WeChat history.",
        }

    def send_message(self, chat_id: str, text: str,
                     idempotency_key: str | None = None) -> dict[str, Any]:
        chat_id, text = chat_id.strip(), text.strip()
        if not chat_id or len(chat_id) > 200:
            raise ValueError("Explicit nonempty chat_id/name (max 200 chars) required")
        if not text or len(text) > 2000:
            raise ValueError("Plain text must be 1-2000 characters")
        if idempotency_key and len(idempotency_key) > 200:
            raise ValueError("idempotency_key too long")

        task = self.store.queue_send(self.account_key, chat_id, text, idempotency_key)
        # One GUI driver per machine. Queue state is persisted *before* UI work.
        with desktop_lock(self.lock_path):
            if not self.store.claim(task["task_id"]):
                return self.store.get_task(task["task_id"]) or task
            try:
                self.adapter.send_text(chat_id, text)
            except AdapterUnavailable as exc:
                self.store.finish(task["task_id"], "failed", str(exc))
            except Exception as exc:
                # May have reached the send button already; never auto-retry.
                self.store.finish(task["task_id"], "uncertain", type(exc).__name__)
            else:
                self.store.finish(
                    task["task_id"], "submitted",
                    "UI automation completed; delivery/recipient confirmation not verified",
                )
        return self.store.get_task(task["task_id"]) or task

    def get_send_status(self, task_id: str) -> dict[str, Any]:
        task = self.store.get_task(task_id)
        return task if task else {"error": "unknown task_id"}


def new_service() -> Service:
    account_key = os.environ.get("WECHAT_PROVIDER_ACCOUNT", "default")
    return Service(Store(default_db_path()), native_adapter(), account_key, configured_reader())
