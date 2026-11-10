"""Manual MCP stdio smoke test; requires installed 'mcp' dependency.

Does not access/send WeChat data. Uses a temporary synthetic index.
Run: .venv/bin/python tests/smoke_mcp.py
"""

import asyncio
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main() -> None:
    with tempfile.TemporaryDirectory() as home:
        command = [sys.executable, '-m', 'wechat_provider.cli']
        params = StdioServerParameters(
            command=command[0],
            args=[*command[1:], "serve"],
            env={**os.environ, "WECHAT_PROVIDER_DB": str(Path(home) / "test.db"),
                 "PYTHONPATH": str(Path(__file__).resolve().parents[1] / 'src')},
        )
        fixture = Path(home) / "synthetic.jsonl"
        fixture.write_text(json.dumps({"chat_id": "synthetic", "source_id": "fixture-1",
            "text": "synthetic import proof", "sender": "fictional",
            "sent_at": "2026-10-01T00:00:00Z", "source": "authorized_export"}) + "\n")
        for expected_imported in (1, 0):
            imported = await asyncio.to_thread(subprocess.run, [*command, "import-jsonl", str(fixture)], env=params.env,
                                      capture_output=True, text=True, check=True)
            assert json.loads(imported.stdout)["imported"] == expected_imported
        async with (stdio_client(params) as (reader, writer),
                    ClientSession(reader, writer) as session):
            await session.initialize()
            available = sorted(tool.name for tool in (await session.list_tools()).tools)
            expected = [
                "wechat_forget_saved_excerpts", "wechat_get_messages",
                "wechat_get_send_status", "wechat_list_chats",
                "wechat_native_export_preview", "wechat_native_export_save",
                "wechat_save_excerpt", "wechat_search", "wechat_send_message",
                "wechat_status", "wechat_storage_inventory",
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
            selected = Path(home) / "inventory-fixture"
            selected.mkdir()
            (selected / "fixture.txt").write_text("synthetic metadata inventory")
            response = await session.call_tool("wechat_storage_inventory", {
                "action": "start", "sources": [{"path": str(selected), "kind": "official_backup"}],
            })
            assert not response.isError
            job = response.structuredContent or json.loads(response.content[0].text)
            for _ in range(100):
                response = await session.call_tool("wechat_storage_inventory", {
                    "action": "report", "job_id": job["job_id"],
                })
                assert not response.isError
                job = response.structuredContent or json.loads(response.content[0].text)
                if job["status"] not in ("queued", "running"):
                    break
                await asyncio.sleep(.05)
            assert job["status"] == "completed"
            assert job["totals"]["file_paths"] == 1
            assert job["totals"]["reclaimable_bytes"] is None
            assert job["sources"][0]["recovery_status"] == "not_verified"
            response = await session.call_tool("wechat_storage_inventory", {
                "action": "analyze", "job_id": job["job_id"],
            })
            assert not response.isError
            analysis = response.structuredContent or json.loads(response.content[0].text)
            assert analysis["summary"]["backup_sources"] == 1
            assert analysis["scan_started"] is False
            assert analysis["source_contents_read"] is False
            response = await session.call_tool("wechat_storage_inventory", {
                "action": "files", "job_id": job["job_id"], "source_id": 1,
                "file_kind": "document", "query": "fixture",
            })
            assert not response.isError
            files = response.structuredContent or json.loads(response.content[0].text)
            assert files["total_file_paths"] == 1
            assert files["scan_started"] is False
            response = await session.call_tool("wechat_storage_inventory", {
                "action": "locate", "job_id": job["job_id"], "source_id": 1,
                "entry_path": files["items"][0]["path"],
            })
            assert not response.isError
            location = response.structuredContent or json.loads(response.content[0].text)
            assert location["identity_checked"] is True
            print("Real CLI import/replay -> MCP indexed read, eleven tools: PASS (synthetic only)")

if __name__ == "__main__":
    asyncio.run(main())
