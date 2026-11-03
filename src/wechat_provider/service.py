"""MCP-facing use cases: no WeChat database writes, no automatic re-send."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

from .adapters import native_adapter
from .greenbubbles_reader import GreenBubblesReader
from .locks import desktop_lock
from .models import AdapterUnavailable, WeChatAdapter
from .native_export import (
    CONTRACT_VERSION,
    ERROR_CHANGED,
    ERROR_INVALID_ARGUMENT,
    ERROR_LIMIT,
    ERROR_UNSUPPORTED,
    FINGERPRINT_RE,
    GRANT_TTL_SECONDS,
    MAX_ARCHIVE_BYTES,
    MAX_MESSAGE_BYTES,
    MAX_MESSAGES,
    MAX_OFFSET,
    MAX_PREVIEW_BYTES,
    MAX_PREVIEW_MESSAGES,
    PLATFORM_SUPPORT,
    PLATFORM_SUPPORTED,
    SOURCE_KIND,
    TOKEN_RE,
    apply_timezone,
    consume_grant,
    default_grant_dir,
    ensure_grant_dir,
    load_native_export,
    render_preview,
)
from .store import Store
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
                 account_key: str = "default", reader=None, grant_dir: Path | None = None):
        self.reader = reader if reader is not None else GreenBubblesReader()
        self.store = store
        self.adapter = adapter
        self.account_key = account_key
        self.lock_path = store.path.with_name("wechat-rpa.lock")
        self.grant_dir = Path(grant_dir) if grant_dir is not None else default_grant_dir(store.path)

    def native_export_status(self) -> dict[str, Any]:
        """Capability report; creates/tightens only the Provider's own grant dir."""
        enabled = bool(PLATFORM_SUPPORTED)
        if enabled:
            try:
                ensure_grant_dir(self.grant_dir)
            except ValueError:
                enabled = False
        return {
            "contract_version": CONTRACT_VERSION,
            "enabled": enabled,
            "platform_support": PLATFORM_SUPPORT,
            "grant_dir": str(self.grant_dir),
            "grant_ttl_seconds": GRANT_TTL_SECONDS,
            "max_archive_bytes": MAX_ARCHIVE_BYTES,
            "max_messages": MAX_MESSAGES,
            "max_preview_messages": MAX_PREVIEW_MESSAGES,
            "max_preview_bytes": MAX_PREVIEW_BYTES,
            "timezone_offset_required_for_absolute_time": True,
        }

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
                "manual_excerpt_save": True,
                "native_export_import": True,
                "native_export_contract_version": CONTRACT_VERSION,
                "structured_db_read": reader["available"],
                "live_read_available": adapter.get("ready_for_attempt", False),
                "live_read_verified": False,
                "send_verified": False,
                "id_kind": "display_name",
            },
            "native_export": self.native_export_status(),
        }

    def save_excerpt(self, chat_title: str, text: str) -> dict[str, Any]:
        """Explicitly save user-selected text to the Provider's own index.

        No WeChat UI/database access. A display label is not a verified contact ID.
        Repeated saves of identical text in the same label are idempotent.
        """
        if (type(chat_title) is not str or type(text) is not str
                or not chat_title.strip() or not text.strip()
                or "\0" in chat_title or "\0" in text):
            raise ValueError("explicit nonempty chat label and text required")
        title, body = chat_title.strip(), text.strip()
        if len(title.encode("utf-8")) > 200 or len(body.encode("utf-8")) > 7800:
            raise ValueError("selected excerpt exceeds local save limit")
        # Historical separator kept so pre-fix saved excerpts stay idempotent.
        digest = hashlib.sha256((title+"\\0"+body).encode("utf-8")).hexdigest()
        row = {"chat_id": title, "source_id": "manual:"+digest,
               "text": body, "sender": "用户主动复制的片段",
               "sent_at": None, "source": "manual_excerpt"}
        result = self.store.import_messages(self.account_key, [row])
        return {"saved": True, "imported": result["imported"],
                "duplicates": result["duplicates"], "source": "local_index",
                "manual_excerpt": True, "chat_title": title,
                "complete": False, "live_read": False}

    def forget_saved_excerpts(self, chat_title: str) -> dict[str, Any]:
        """Remove only user-saved excerpts for this label, not imported history."""
        if (type(chat_title) is not str or not chat_title.strip()
                or len(chat_title.strip().encode("utf-8")) > 200 or "\0" in chat_title):
            raise ValueError("invalid chat label")
        removed = self.store.delete_manual_excerpts(self.account_key, chat_title.strip())
        return {"removed": removed, "source": "local_index", "chat_title": chat_title.strip(),
                "manual_excerpt_only": True}

    def preview_native_export(self, grant_token: str,
                              timezone_offset_minutes: int | None = None,
                              offset: int = 0, limit: int = MAX_PREVIEW_MESSAGES) -> dict[str, Any]:
        """Bounded preview of a user-selected merged-forward ZIP (no persistence)."""
        if not PLATFORM_SUPPORTED:
            raise ValueError(ERROR_UNSUPPORTED)
        if type(grant_token) is not str or not TOKEN_RE.fullmatch(grant_token):
            raise ValueError(ERROR_INVALID_ARGUMENT)
        if timezone_offset_minutes is not None and (
                type(timezone_offset_minutes) is not int
                or not -840 <= timezone_offset_minutes <= 840):
            raise ValueError(ERROR_INVALID_ARGUMENT)
        if type(offset) is not int or not 0 <= offset <= MAX_OFFSET:
            raise ValueError(ERROR_INVALID_ARGUMENT)
        if type(limit) is not int or not 1 <= limit <= MAX_PREVIEW_MESSAGES:
            raise ValueError(ERROR_INVALID_ARGUMENT)
        if offset + limit > MAX_MESSAGES:
            raise ValueError(ERROR_INVALID_ARGUMENT)
        zip_path = consume_grant(self.grant_dir, grant_token, "preview")
        export = load_native_export(zip_path)
        if offset > len(export.records):
            raise ValueError(ERROR_INVALID_ARGUMENT)
        if any(len(record.text.encode("utf-8")) > MAX_MESSAGE_BYTES for record in export.records):
            raise ValueError(ERROR_LIMIT)
        already_saved = self.store.get_native_export(export.fingerprint) is not None
        return render_preview(
            export,
            timezone_offset_minutes=timezone_offset_minutes,
            offset=offset,
            limit=limit,
            already_saved=already_saved,
        )

    def save_native_export(self, grant_token: str, expected_fingerprint: str,
                           timezone_offset_minutes: int | None = None) -> dict[str, Any]:
        """Explicitly import a previewed export into the existing Provider index.

        With an explicit ``timezone_offset_minutes`` saved rows get an offset-aware
        ``sent_at`` and ``native_exports.timezone_offset_minutes`` is set; without
        one ``sent_at`` stays NULL (the local timestamp is never silently converted).
        A fingerprint already recorded is a no-op: nothing is re-inserted and stored
        timestamps are never rewritten, so re-saving with a different offset cannot
        raise an identity conflict.
        """
        if not PLATFORM_SUPPORTED:
            raise ValueError(ERROR_UNSUPPORTED)
        if type(grant_token) is not str or not TOKEN_RE.fullmatch(grant_token):
            raise ValueError(ERROR_INVALID_ARGUMENT)
        if type(expected_fingerprint) is not str or not FINGERPRINT_RE.fullmatch(expected_fingerprint):
            raise ValueError(ERROR_INVALID_ARGUMENT)
        if timezone_offset_minutes is not None and (
                type(timezone_offset_minutes) is not int
                or not -840 <= timezone_offset_minutes <= 840):
            raise ValueError(ERROR_INVALID_ARGUMENT)
        zip_path = consume_grant(self.grant_dir, grant_token, "save")
        export = load_native_export(zip_path)
        if export.fingerprint != expected_fingerprint:
            # TOCTOU guard: the archive changed after preview; import nothing.
            raise ValueError(ERROR_CHANGED)
        recorded = self.store.get_native_export(export.fingerprint)
        if recorded is not None:
            # Already recorded: re-inserting would conflict when the offsets differ
            # and could rewrite `sent_at`; both are forbidden, so this is a bounded
            # no-op that reports the offset actually stored for this export.
            return self._native_export_receipt(
                export,
                imported=0,
                duplicates=recorded["message_count"],
                already_imported=True,
                timezone_offset_minutes=recorded["timezone_offset_minutes"],
            )
        rows = [{
            "chat_id": export.session_id,
            "source_id": record.source_id,
            "text": record.text,
            "sender": record.sender,
            "sent_at": (apply_timezone(record.local_time, timezone_offset_minutes)
                        if timezone_offset_minutes is not None else None),
            "source": SOURCE_KIND,
        } for record in export.records]
        result = self.store.import_messages(self.account_key, rows)
        self.store.record_native_export({
            "session_id": export.session_id,
            "fingerprint": export.fingerprint,
            "label": export.label,
            "message_count": len(export.records),
            "participant_count": len(export.participants),
            "attachment_count": len(export.attachments),
            "timezone_offset_minutes": timezone_offset_minutes,
            "source_zip_name": export.archive_name,
        })
        return self._native_export_receipt(
            export,
            imported=result["imported"],
            duplicates=result["duplicates"],
            already_imported=False,
            timezone_offset_minutes=timezone_offset_minutes,
        )

    @staticmethod
    def _native_export_receipt(export, *, imported: int, duplicates: int,
                               already_imported: bool,
                               timezone_offset_minutes: int | None) -> dict[str, Any]:
        timezone_field: dict[str, Any] = (
            {"applied": True, "offset_minutes": timezone_offset_minutes}
            if timezone_offset_minutes is not None else {"applied": False}
        )
        return {
            "ok": True,
            "saved": True,
            "source": SOURCE_KIND,
            "session_id": export.session_id,
            "fingerprint": export.fingerprint,
            "imported": imported,
            "duplicates": duplicates,
            "already_imported": already_imported,
            "message_count": len(export.records),
            "attachment_count": len(export.attachments),
            "timezone": timezone_field,
            "live_read": False,
            "complete": False,
        }

    def archive_native_export(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """Reserved for P1.5 (Markdown/attachment export); not implemented in P0."""
        raise ValueError(ERROR_UNSUPPORTED)

    # IPC whitelist names (``local_ipc.METHODS``) are the stable transport surface.
    native_export_preview = preview_native_export
    native_export_save = save_native_export

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
        # An unavailable desktop reader is an error, never an empty list:
        # reporting [] would let callers imply coverage/emptiness we do not know.
        with desktop_lock(self.lock_path):
            chats = self.adapter.list_chats(limit)
        return {"chats": chats, "source": "desktop_ui", "id_kind": "display_name",
                "complete": False, "live_read": False}

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
            except Exception as exc:  # noqa: BLE001  # unknown UI failure is never auto-retried
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
