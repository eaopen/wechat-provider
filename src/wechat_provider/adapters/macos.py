"""macOS native accessibility adapter via PatrickHua/wxauto-mac (optional).

The library has primarily historical compatibility claims. Current WeChat builds
must be verified on a real machine before treating it as production-ready.
"""

from __future__ import annotations

import importlib.util
from typing import Any

from wechat_provider.models import AdapterUnavailable


class MacAdapter:
    backend = "wxauto-mac"

    def status(self) -> dict[str, Any]:
        installed = importlib.util.find_spec("wxauto_mac") is not None
        return {
            "platform": "macos",
            "backend": self.backend,
            "backend_installed": installed,
            "ui_logged_in": "unknown",
            "ready_for_attempt": False,
            "live_read_available": False,
            "send_available": False,
            "blocked_reason": "macos_login_safety_hold",
            "note": "Live automation paused after relogin incidents; local index remains available.",
        }

    @staticmethod
    def _client():
        # Do not instantiate wxauto_mac: initialization itself activates WeChat.
        raise AdapterUnavailable(
            "macos_login_safety_hold: live UI disabled pending compatibility audit; "
            "use explicit local_index reads or manual paste"
        )

    def list_chats(self, limit: int) -> list[dict[str, Any]]:
        sessions = self._client().GetSessionList()
        output = []
        for item in sessions[:limit]:
            name = str(item.get("name") or "") if isinstance(item, dict) else str(item)
            if not name:
                continue
            output.append({
                "chat_id": name,
                "title": name,
                "last_text": item.get("last_message") if isinstance(item, dict) else None,
                "unread_count": item.get("unread_count") if isinstance(item, dict) else None,
                "id_kind": "display_name",
            })
        return output

    def get_messages(self, chat_id: str, limit: int) -> list[dict[str, Any]]:
        history = self._client().GetChatHistory(chat_id)
        return [
            {"text": str(text), "sender": None, "sent_at": None,
             "source": "macos_ui"}
            for text in history[-limit:] if text
        ]

    def send_text(self, chat_id: str, text: str) -> None:
        # wxauto-mac returns bool after AppleScript, without recipient delivery proof.
        if not self._client().SendToFriend(chat_id, text):
            raise RuntimeError(
                "macOS send not confirmed by wxauto-mac; check WeChat UI manually"
            )
