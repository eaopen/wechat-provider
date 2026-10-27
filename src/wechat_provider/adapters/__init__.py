"""Select native clients. A small subprocess keeps noisy GUI libraries off MCP stdout."""

from __future__ import annotations

import sys

from wechat_provider.models import WeChatAdapter


def direct_adapter(platform: str | None = None) -> WeChatAdapter:
    """Only called within the UI subprocess (or adapter-shape tests)."""
    current = platform or sys.platform
    if current == "win32":
        from .windows import WindowsAdapter
        return WindowsAdapter()
    if current == "darwin":
        from .macos import MacAdapter
        return MacAdapter()
    from .unsupported import UnsupportedAdapter
    return UnsupportedAdapter(current)


def native_adapter(platform: str | None = None) -> WeChatAdapter:
    current = platform or sys.platform
    if current in {"darwin", "win32"}:
        from .worker import WorkerAdapter
        return WorkerAdapter(current)
    return direct_adapter(current)
