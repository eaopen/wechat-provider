"""Standalone stdio MCP tools. Local Bridge may register this command later.

Only imported when the optional core MCP dependency is installed.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from .service import new_service

mcp = FastMCP("wechat-provider")
_service = None


def service():
    global _service
    if _service is None:
        _service = new_service()
    return _service


@mcp.tool()
def wechat_status() -> dict:
    """Check adapter installation/readiness; does not claim the user is logged in."""
    return service().status()


@mcp.tool()
def wechat_list_chats(limit: int = 30, source: str = "desktop_ui", cursor: str | None = None) -> dict:
    """List desktop_ui/local_index chats or structured_db stable IDs; cursor only for structured_db."""
    return service().list_chats(limit, source, cursor)


@mcp.tool()
def wechat_get_messages(chat_id: str, limit: int = 50, source: str = "desktop_ui", cursor: str | None = None) -> dict:
    """Read named desktop/index chats or a stable structured_db conversation ID. No automatic fallback across readers."""
    return service().get_messages(chat_id, limit, source, cursor)


@mcp.tool()
def wechat_search(query: str, chat_id: str | None = None, limit: int = 30, source: str = "local_index", cursor: str | None = None) -> dict:
    """Search local_index by default or explicit structured_db; neither guarantees full history."""
    return service().search(query, chat_id, limit, source, cursor)


@mcp.tool()
def wechat_send_message(chat_id: str, text: str,
                        idempotency_key: str | None = None) -> dict:
    """Submit a message through desktop RPA. Requires user's authorized intent.

    chat_id is currently a display name: confirm recipient identity manually
    in ambiguous cases. 'submitted' is NOT verified delivery. Never blind retry.
    """
    return service().send_message(chat_id, text, idempotency_key)


@mcp.tool()
def wechat_get_send_status(task_id: str) -> dict:
    """Read the persisted state of a local sending attempt."""
    return service().get_send_status(task_id)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
