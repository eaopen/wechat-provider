"""Manual MCP stdio smoke test; requires installed 'mcp' dependency.

Does not access/send WeChat data. Uses a temporary synthetic index.
Run: .venv/bin/python tests/smoke_mcp.py
"""

import asyncio
import os
import json
import subprocess
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
        fixture = Path(home) / "synthetic.jsonl"
        fixture.write_text(json.dumps({"chat_id": "synthetic", "source_id": "fixture-1",
            "text": "synthetic import proof", "sender": "fictional",
            "sent_at": "2026-10-01T00:00:00Z", "source": "authorized_export"}) + "\n")
        for expected_imported in (1, 0):
            imported = subprocess.run([command, "import-jsonl", str(fixture)], env=params.env,
                                      capture_output=True, text=True, check=True)
            assert json.loads(imported.stdout)["imported"] == expected_imported
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
                for tool, args in [("wechat_list_chats", {"source": "local_index"}),
                                   ("wechat_get_messages", {"chat_id": "synthetic", "source": "local_index"})]:
                    indexed = await session.call_tool(tool, args)
                    assert not indexed.isError, indexed
                result = await session.call_tool("wechat_get_messages",
                    {"chat_id": "synthetic", "source": "local_index"})
                payload = result.structuredContent
                if payload is None:
                    payload = json.loads(result.content[0].text)
                assert payload["live_read"] is False
                assert len(payload["messages"]) == 1
                assert payload["messages"][0]["source_id"] == "fixture-1"
                assert payload["messages"][0]["source"] == "authorized_export"
                print("Real CLI import/replay -> MCP indexed read, six tools: PASS (synthetic only)")


if __name__ == "__main__":
    asyncio.run(main())
