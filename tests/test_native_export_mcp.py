"""Real MCP stdio integration for the native-share tools (synthetic ZIP only).

Runs the actual ``wechat-provider serve`` command through the MCP client over
stdio in an isolated temp data directory, then exercises grant -> preview ->
save -> local_index provenance and asserts the contract shape. No WeChat
database, login state or real chat text is used.
"""

import asyncio
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from wechat_provider.native_export import mint_grant
from wechat_provider.store import Store

TRANSCRIPT = "聊天记录.txt"
EXPECTED_TOOLS = {
    "wechat_status",
    "wechat_list_chats",
    "wechat_get_messages",
    "wechat_search",
    "wechat_send_message",
    "wechat_get_send_status",
    "wechat_save_excerpt",
    "wechat_forget_saved_excerpts",
    "wechat_native_export_preview",
    "wechat_native_export_save",
}


def record(sender, stamp, body):
    return f"·{sender}\n{stamp}\n{body}\n\n"


def transcript(*items):
    return "".join(record(*item) for item in items).encode("utf-8")


def payload(result):
    """Return (object, error_text) from a CallToolResult."""
    if result.isError:
        text = " ".join(getattr(block, "text", "") for block in result.content or [])
        return None, text
    if isinstance(result.structuredContent, dict):
        return result.structuredContent, ""
    return json.loads(result.content[0].text), ""


class NativeExportMcpStdioTest(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name).resolve()
        self.db_path = self.root / "provider" / "wechat.db"
        self.grant_dir = self.db_path.parent / "native-export-grants"
        self.repo_root = Path(__file__).resolve().parents[1]
        self.zip_path = self.root / "项目群的聊天记录.zip"
        with zipfile.ZipFile(self.zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(TRANSCRIPT, transcript(
                ("张三", "2026年9月5日 08:05", "早上好 ☕️\n今天有点忙"),
                ("我", "2026年9月5日 08:06", "好的"),
                ("张三", "2026年9月5日 08:07", "OK 👌"),
            ))
            archive.writestr("images/photo.png", b"\x89PNG\r\n\x1a\nsynthetic")
        self.unsafe_zip = self.root / "traversal.zip"
        with zipfile.ZipFile(self.unsafe_zip, "w") as archive:
            archive.writestr(TRANSCRIPT, transcript(("甲", "2026年9月5日 08:05", "hi")))
            archive.writestr("../evil.png", b"x")

    def grant(self, purpose="preview", path=None) -> str:
        return mint_grant(self.grant_dir, path or self.zip_path, purpose)["grant_token"]

    def run_stdio(self, flow):
        """Run one MCP stdio session against the real provider CLI."""
        environment = {
            **os.environ,
            "PYTHONPATH": str(self.repo_root / "src"),
            "WECHAT_PROVIDER_DB": str(self.db_path),
            "WECHAT_PROVIDER_READER": "unconfigured",
        }
        parameters = StdioServerParameters(
            command=os.sys.executable,
            args=["-m", "wechat_provider.cli", "serve"],
            env=environment,
            cwd=str(self.repo_root),
        )

        async def session_flow():
            async with stdio_client(parameters) as (read_stream, write_stream), \
                    ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                return await flow(session)

        return asyncio.run(session_flow())

    def test_full_grant_preview_save_flow_over_stdio(self):
        state = {}

        async def flow(session):
            tools = await session.list_tools()
            state["tools"] = {tool.name for tool in tools.tools}

            preview, error = payload(await session.call_tool(
                "wechat_native_export_preview",
                {"grant_token": self.grant(), "timezone_offset_minutes": 480,
                 "offset": 0, "limit": 2},
            ))
            state["preview"] = preview
            state["preview_error"] = error
            state["preview_token"] = None

            no_zone_token = self.grant()
            no_zone, no_zone_error = payload(await session.call_tool(
                "wechat_native_export_preview",
                {"grant_token": no_zone_token, "timezone_offset_minutes": None,
                 "offset": 0, "limit": 200}))
            state["no_zone"] = no_zone
            state["no_zone_error"] = no_zone_error

            unsafe, unsafe_error = payload(await session.call_tool(
                "wechat_native_export_preview", {"grant_token": self.grant(path=self.unsafe_zip)}))
            state["unsafe"] = unsafe
            state["unsafe_error"] = unsafe_error

            replay, replay_error = payload(await session.call_tool(
                "wechat_native_export_preview", {"grant_token": no_zone_token}))
            state["replay"] = replay
            state["replay_error"] = replay_error

            fingerprint = preview["export"]["fingerprint"]
            saved, saved_error = payload(await session.call_tool(
                "wechat_native_export_save",
                {"grant_token": self.grant("save"), "expected_fingerprint": fingerprint}))
            state["saved"] = saved
            state["saved_error"] = saved_error
            again, again_error = payload(await session.call_tool(
                "wechat_native_export_save",
                {"grant_token": self.grant("save"), "expected_fingerprint": fingerprint}))
            state["again"] = again
            state["again_error"] = again_error

            chats, chats_error = payload(await session.call_tool(
                "wechat_list_chats", {"source": "local_index", "limit": 20}))
            state["chats"] = chats
            state["chats_error"] = chats_error
            history, history_error = payload(await session.call_tool(
                "wechat_get_messages",
                {"chat_id": preview["export"]["session_id"], "source": "local_index",
                 "limit": 20}))
            state["history"] = history
            state["history_error"] = history_error
            status, status_error = payload(await session.call_tool("wechat_status", {}))
            state["status"] = status
            state["status_error"] = status_error
            return True

        self.assertTrue(self.run_stdio(flow))

        self.assertEqual(state["tools"], EXPECTED_TOOLS)
        preview = state["preview"]
        self.assertEqual(state["preview_error"], "")
        self.assertTrue(preview["ok"])
        self.assertEqual(preview["source"], "native_share")
        export = preview["export"]
        self.assertEqual(export["contract_version"], 1)
        self.assertTrue(export["session_id"].startswith("native_share:"))
        self.assertRegex(export["fingerprint"], r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(export["label"], "项目群的聊天记录")
        self.assertEqual(export["participants"], ["张三", "我"])
        self.assertEqual(export["participant_count"], 2)
        self.assertEqual(export["message_count"], 3)
        self.assertEqual(export["attachment_count"], 1)
        self.assertEqual(export["attachments"][0]["kind"], "image")
        self.assertEqual(export["timezone"], {"applied": True, "offset_minutes": 480})
        self.assertEqual(export["coverage"]["format"], "native_txt")
        self.assertEqual(export["coverage"]["parsed_records"], 3)
        self.assertFalse(export["already_saved"])
        self.assertEqual(preview["window"],
                         {"offset": 0, "limit": 2, "returned": 2,
                          "has_more": True, "next_offset": 2})
        self.assertEqual([message["index"] for message in preview["messages"]], [0, 1])
        message = preview["messages"][0]
        self.assertEqual(message["sender"], "张三")
        self.assertEqual(message["sent_at"], "2026-09-05T08:05:00+08:00")
        self.assertEqual(message["sent_at_local"], "2026-09-05T08:05")
        self.assertIn("☕️", message["text"])
        self.assertIn("\n", message["text"])
        self.assertTrue(message["source_id"].startswith("native:"))

        no_zone = state["no_zone"]
        self.assertEqual(state["no_zone_error"], "")
        self.assertEqual(no_zone["export"]["timezone"], {"applied": False})
        self.assertIsNone(no_zone["messages"][0]["sent_at"])
        self.assertEqual(no_zone["messages"][0]["sent_at_local"], "2026-09-05T08:05")
        self.assertTrue(no_zone["warnings"])

        self.assertIsNone(state["unsafe"])
        self.assertIn("native_export_archive_invalid", state["unsafe_error"])
        self.assertIsNone(state["replay"])
        self.assertIn("native_export_grant_invalid", state["replay_error"])

        saved = state["saved"]
        self.assertEqual(state["saved_error"], "")
        self.assertTrue(saved["saved"])
        self.assertEqual(saved["source"], "native_share")
        self.assertEqual(saved["imported"], 3)
        self.assertEqual(saved["duplicates"], 0)
        self.assertFalse(saved["already_imported"])
        self.assertFalse(saved["live_read"])
        self.assertFalse(saved["complete"])
        self.assertEqual(saved["fingerprint"], export["fingerprint"])
        self.assertEqual(saved["session_id"], export["session_id"])

        again = state["again"]
        self.assertEqual(state["again_error"], "")
        self.assertEqual(again["imported"], 0)
        self.assertEqual(again["duplicates"], 3)
        self.assertTrue(again["already_imported"])

        chats = state["chats"]
        self.assertEqual(state["chats_error"], "")
        row = next(item for item in chats["chats"]
                   if item["chat_id"] == export["session_id"])
        self.assertEqual(row["title"], "项目群的聊天记录")
        self.assertEqual(row["source_kind"], "native_share")
        self.assertTrue(row["native_share_only"])
        self.assertEqual(row["message_count"], 3)

        history = state["history"]
        self.assertEqual(state["history_error"], "")
        self.assertFalse(history["live_read"])
        self.assertEqual(len(history["messages"]), 3)
        self.assertEqual({item["source"] for item in history["messages"]}, {"native_share"})

        status = state["status"]
        self.assertEqual(state["status_error"], "")
        self.assertTrue(status["capabilities"]["native_export_import"])
        self.assertEqual(status["native_export"]["contract_version"], 1)
        self.assertEqual(status["native_export"]["grant_dir"], str(self.grant_dir))

        stored = Store(self.db_path)
        rows = stored.get_messages("default", export["session_id"], 10)
        self.assertEqual(len(rows), 3)
        self.assertEqual({row["source"] for row in rows}, {"native_share"})
        metadata = stored.get_native_export(export["fingerprint"])
        self.assertIsNotNone(metadata)
        self.assertEqual(metadata["label"], "项目群的聊天记录")

    def test_stdio_save_fingerprint_mismatch_is_an_error(self):
        async def flow(session):
            receipt, error = payload(await session.call_tool(
                "wechat_native_export_save",
                {"grant_token": self.grant("save"),
                 "expected_fingerprint": "sha256:" + "0" * 64}))
            return receipt, error

        receipt, error = self.run_stdio(flow)
        self.assertIsNone(receipt)
        self.assertIn("native_export_changed", error)
        self.assertEqual(Store(self.db_path).get_messages("default", "native_share:", 10), [])


if __name__ == "__main__":
    unittest.main()
