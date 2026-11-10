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
def wechat_save_excerpt(chat_title: str, text: str) -> dict:
    """Save user-selected copied text to a labeled *local* index only.

    Caller must obtain an explicit foreground user action; not a WeChat history
    read, not a verified contact identity and not a message send.
    """
    return service().save_excerpt(chat_title, text)


@mcp.tool()
def wechat_forget_saved_excerpts(chat_title: str) -> dict:
    """On explicit user action, delete only manually saved local copies."""
    return service().forget_saved_excerpts(chat_title)


@mcp.tool()
def wechat_native_export_preview(grant_token: str,
                                 timezone_offset_minutes: int | None = None,
                                 offset: int = 0, limit: int = 200) -> dict:
    """Bounded preview of a user-selected WeChat merged-forward ZIP.

    The caller never passes a path or bytes: Companion writes a one-time grant
    file after an explicit user gesture. Without `timezone_offset_minutes`,
    `sent_at` stays null (the local timestamp is never silently converted).
    """
    return service().native_export_preview(grant_token, timezone_offset_minutes, offset, limit)


@mcp.tool()
def wechat_native_export_save(grant_token: str, expected_fingerprint: str,
                              timezone_offset_minutes: int | None = None) -> dict:
    """Explicitly import a previewed export into the local index (idempotent).

    `expected_fingerprint` must echo the preview; a changed ZIP is rejected.
    With `timezone_offset_minutes` the saved rows keep an offset-aware `sent_at`
    and the offset is recorded; without it `sent_at` stays null (never assumed).
    An already-recorded fingerprint is a no-op that does not rewrite timestamps.
    Attachments stay metadata-only in P0.
    """
    return service().native_export_save(
        grant_token, expected_fingerprint, timezone_offset_minutes)


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


@mcp.tool()
def wechat_storage_inventory(action: str, sources: list[dict] | None = None,
                             job_id: str | None = None, max_entries: int = 250000,
                             source_id: int | None = None, query: str = "",
                             file_kind: str | None = None, sharing: str = "all",
                             offset: int = 0, limit: int = 50, entry_path: str | None = None) -> dict:
    """Inventory explicitly user-selected directories, without reading file contents.

    action: discover/start/report/analyze/files/locate/cancel/resume. discover checks known macOS WeChat
    locations and recommends sources, without starting an inventory. start sources: [{path: absolute directory,
    kind: active_data/official_backup/historical_copy/cache_downloads/other}].
    Starts a bounded metadata worker and saves private inventory state. No source
    mutation, content hashing, backup restore checks or AI analysis. analyze
    triages backups from the finished saved inventory, without source reads or
    rescanning; file mtimes and hardlink sharing do not prove chat coverage. Other
    governance operations are separate capabilities, not exposed by this policy.
    files filters saved filenames (query literal, file_kind, sharing all/shared/
    not_observed_shared); at most 50 paths ordered by size per page, no content
    reads. locate validates a saved source_id/entry_path identity and returns its
    local path; it does not open the file or launch Finder itself.
    """
    return service().storage_inventory(action, sources, job_id, max_entries,
        source_id, query, file_kind, sharing, offset, limit, entry_path)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
