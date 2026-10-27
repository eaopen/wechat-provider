"""Small adapter contract. Keep platform-specific APIs out of the service."""

from __future__ import annotations

from typing import Any, Protocol


class AdapterUnavailable(RuntimeError):
    """Required desktop app, permission, or optional library is unavailable."""


class WeChatAdapter(Protocol):
    def status(self) -> dict[str, Any]:
        """Capabilities and readiness; never assume status implies logged in."""

    def list_chats(self, limit: int) -> list[dict[str, Any]]:
        """UI-visible sessions; display name may not be a stable identity."""

    def get_messages(self, chat_id: str, limit: int) -> list[dict[str, Any]]:
        """Read only messages accessible through the installed client."""

    def send_text(self, chat_id: str, text: str) -> None:
        """Submit a text send through the user's interactive WeChat client.

        Returning without error does NOT prove delivery.
        """
