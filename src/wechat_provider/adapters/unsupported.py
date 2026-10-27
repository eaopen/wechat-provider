"""Unsupported platform: import/search remain available, GUI actions unavailable."""

from __future__ import annotations

from typing import Any

from wechat_provider.models import AdapterUnavailable


class UnsupportedAdapter:
    def __init__(self, platform: str):
        self.platform = platform

    def status(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "backend": "none",
            "backend_installed": False,
            "ui_logged_in": False,
            "ready_for_attempt": False,
            "note": "GUI RPA currently supports macOS and Windows only.",
        }

    def list_chats(self, limit: int) -> list[dict[str, Any]]:
        raise AdapterUnavailable("Desktop WeChat RPA is only supported on macOS/Windows")

    def get_messages(self, chat_id: str, limit: int) -> list[dict[str, Any]]:
        raise AdapterUnavailable("Desktop WeChat RPA is only supported on macOS/Windows")

    def send_text(self, chat_id: str, text: str) -> None:
        raise AdapterUnavailable("Desktop WeChat RPA is only supported on macOS/Windows")
