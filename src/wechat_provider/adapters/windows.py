"""Adapter for the maintained pyweixin module of Hello-Mr-Crab/pywechat."""

from __future__ import annotations

import importlib.util
from typing import Any

from wechat_provider.models import AdapterUnavailable


class WindowsAdapter:
    backend = "pyweixin"

    def status(self) -> dict[str, Any]:
        installed = importlib.util.find_spec("pyweixin") is not None
        return {
            "platform": "windows",
            "backend": self.backend,
            "backend_installed": installed,
            "ui_logged_in": "unknown",
            "ready_for_attempt": installed,
            "note": "UI tree and session must be checked on real WeChat 4.x before use.",
        }

    @staticmethod
    def _messages():
        try:
            from pyweixin import Messages
            return Messages
        except ImportError as exc:
            raise AdapterUnavailable(
                "Missing pyweixin; on Windows install optional dependency: "
                'pip install "wechat-provider[windows]"'
            ) from exc

    def list_chats(self, limit: int) -> list[dict[str, Any]]:
        sessions = self._messages().dump_recent_sessions(
            recent="Week", close_weixin=False
        )
        result = []
        for item in sessions[:limit]:
            if not item:
                continue
            if isinstance(item, (tuple, list)):
                name = str(item[0])
                last_at = str(item[1]) if len(item) > 1 else None
                last_text = str(item[2]) if len(item) > 2 else None
            else:
                name, last_at, last_text = str(item), None, None
            result.append({
                "chat_id": name, "title": name, "last_at": last_at,
                "last_text": last_text, "id_kind": "display_name",
            })
        return result

    def get_messages(self, chat_id: str, limit: int) -> list[dict[str, Any]]:
        # UI history, not a complete database or an immutable message stream.
        messages = self._messages().pull_messages(
            friend=chat_id, number=limit, close_weixin=False
        )
        output = []
        for row in messages or []:
            if not isinstance(row, dict):
                continue
            content = row.get("消息内容") or row.get("text")
            if not content:
                continue
            output.append({
                "text": str(content),
                "sender": row.get("消息发送人") or row.get("sender"),
                "sent_at": row.get("消息发送时间") or row.get("sent_at"),
                "source": "windows_ui",
            })
        return output[-limit:]

    def send_text(self, chat_id: str, text: str) -> None:
        # Method returns None after UI steps; this is SUBMITTED, not delivered.
        self._messages().send_messages_to_friend(
            friend=chat_id,
            messages=[text],
            clear=True,
            search_pages=0,
            close_weixin=False,
        )
