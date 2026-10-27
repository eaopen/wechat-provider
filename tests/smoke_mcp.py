"""Manual MCP stdio smoke test; requires installed 'mcp' dependency.

Does not access/send WeChat data. Uses a temporary empty index.
Run: .venv/bin/python tests/smoke_mcp.py
"""

import asyncio
import os
import tempfile
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main() -> None:
    with tempfile.TemporaryDirectory() as home:
        command = str(Path(__file__).resolve().parent.parent / ".venv" / "bin" / "wechat-provider")
        params = StdioServerParameters(
            command=command,
            args=["serve"],
            env={**os.environ, "WECHAT_PROVIDER_DB": str(Path(home) / "test.db")},
        )
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                available = sorted(tool.name for tool in (await session.list_tools()).tools)
                expected = [
                    "wechat_get_messages", "wechat_get_send_status", "wechat_list_chats",
                    "wechat_search", "wechat_send_message", "wechat_status",
                ]
                assert available == expected, available
                result = await session.call_tool("wechat_status", {})
                assert not result.isError, result
                query = await session.call_tool("wechat_search", {"query": "不存在的示例"})
                assert not query.isError, query
                print("MCP stdio handshake, six tools and read-only status/search: PASS")


if __name__ == "__main__":
    asyncio.run(main())
