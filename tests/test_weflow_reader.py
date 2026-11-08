import asyncio
import base64
import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from test_chatlab_import import document

from wechat_provider.chatlab_import import index_chat_id
from wechat_provider.models import AdapterUnavailable
from wechat_provider.service import Service
from wechat_provider.store import Store
from wechat_provider.wechat_mcp_reader import configured_reader
from wechat_provider.weflow_reader import WeFlowReader, sync_weflow


class WeFlowTest(unittest.TestCase):
    def setUp(self):
        self.requests = []
        self.mode = "normal"
        self.fail_offset = None
        self.end = int(time.time()) - 5
        self.messages = document(("1", "2", "3", "4", "5"))["messages"]
        for row in self.messages:
            row["timestamp"] = self.end - 1  # More equal-timestamp messages than one page.
        self.messages[-1]["timestamp"] = self.end
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                url = urlsplit(self.path)
                params = parse_qs(url.query)
                outer.requests.append((url.path, params, self.headers.get("Authorization")))
                if outer.mode == "redirect":
                    self.send_response(302)
                    self.send_header(
                        "Location", "/forbidden-write-route?access_token=PRIVATE_TOKEN"
                    )
                    self.end_headers()
                    return
                if outer.mode == "auth":
                    self.send_error(401, "PRIVATE_TOKEN_AND_BODY")
                    return
                if url.path == "/api/v1/sessions":
                    data = {"sessions": [{"id": "group-a@chatroom", "name": "虚构同名会话"}]}
                else:
                    offset = int(params["offset"][0])
                    if outer.fail_offset == offset:
                        self.send_error(503, "PRIVATE_TOKEN_AND_BODY")
                        return
                    since, end = int(params["since"][0]), int(params["end"][0])
                    rows = [row for row in outer.messages if since <= row["timestamp"] <= end]
                    limit = int(params["limit"][0])
                    selected = rows[offset : offset + limit]
                    data = document(())
                    data["messages"] = selected
                    more = offset + len(selected) < len(rows)
                    data["sync"] = {
                        "hasMore": more,
                        "nextOffset": offset + len(selected),
                        "nextSince": selected[-1]["timestamp"] if selected else None,
                        "watermark": int(time.time()),
                    }
                    if outer.mode == "stalled":
                        data["sync"].update(hasMore=True, nextOffset=offset)
                    if outer.mode == "owner":
                        data["meta"]["ownerId"] = "wrong-owner"
                    if outer.mode == "invalid":
                        data = {"success": False, "error": "PRIVATE_TOKEN_AND_BODY"}
                raw = json.dumps(data).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.adapter = Mock()
        self.service = Service(Store(self.root / "index.db"), self.adapter, "synthetic")
        self.reader = WeFlowReader(
            f"http://127.0.0.1:{self.server.server_port}",
            "PRIVATE_TOKEN",
            "fixture",
            "fixture-owner",
        )

    def test_literal_loopback_config_secrets_and_offline_status(self):
        for url in (
            "http://localhost:5031",
            "https://example.com:443",
            "http://127.0.0.1:5031/api",
            "http://u:p@127.0.0.1:5031",
            "http://127.0.0.1:5031?token=secret",
            "http://127.0.0.1:bad",
            "http://127.0.0.1:5031#fragment",
        ):
            reader = WeFlowReader(url, "PRIVATE_TOKEN", "fixture", "fixture-owner")
            self.assertFalse(reader.status()["available"])
            with self.assertRaises(AdapterUnavailable):
                reader.query("conversations.list")
        self.assertTrue(self.reader.status()["available"])
        self.assertNotIn("PRIVATE_TOKEN", json.dumps(self.reader.status()))
        self.assertEqual(self.requests, [])
        with patch.dict(os.environ, {"WECHAT_PROVIDER_READER": "weflow"}):
            self.assertIsInstance(configured_reader(), WeFlowReader)

    def test_structured_source_routes_and_cursor_binding(self):
        self.service.reader = self.reader
        self.assertEqual(
            self.service.list_chats(source="structured_db")["chats"][0]["chat_id"],
            "group-a@chatroom",
        )
        result = self.service.get_messages("group-a@chatroom", 2, source="structured_db")
        self.assertTrue(result["has_more"])
        self.assertFalse(result["complete"])
        with self.assertRaises(ValueError):
            self.reader.page("other-session", 2, result["next_cursor"])
        with self.assertRaises(AdapterUnavailable):
            self.service.search("private query", source="structured_db")
        self.assertEqual(self.adapter.mock_calls, [])
        self.assertEqual(self.service.list_chats(source="local_index")["chats"], [])

    def test_equal_timestamp_bounded_resume_replay_and_incremental_boundary(self):
        first = sync_weflow(self.service, self.reader, "group-a@chatroom", 1, 2)
        self.assertEqual((first["imported"], first["has_more"]), (2, True))
        # Recreate both reader and Store to prove persistence, not just in-memory continuation.
        self.service.store = Store(self.root / "index.db")
        second = sync_weflow(self.service, self.reader, "group-a@chatroom", 10, 2)
        self.assertEqual((second["imported"], second["has_more"]), (3, False))
        pulls = [params for path, params, _ in self.requests if path.endswith("/messages")]
        self.assertEqual([p["offset"] for p in pulls], [["0"], ["2"], ["4"]])
        self.assertEqual(len({p["since"][0] for p in pulls}), 1)
        self.assertEqual(len({p["end"][0] for p in pulls}), 1)
        # A late same-boundary-second arrival must be found by the overlap window.
        boundary = int(pulls[0]["end"][0])
        self.messages.append(
            {
                **self.messages[-1],
                "platformMessageId": "6",
                "content": "合成合同 6",
                "timestamp": boundary,
            }
        )
        third = sync_weflow(self.service, self.reader, "group-a@chatroom", 10, 2)
        self.assertEqual(third["imported"], 1)
        fourth = sync_weflow(self.service, self.reader, "group-a@chatroom", 10, 2)
        self.assertEqual(fourth["imported"], 0)
        self.assertEqual(len(self.service.search("合成合同")["matches"]), 6)
        for path, params, token in self.requests:
            self.assertEqual(token, "Bearer PRIVATE_TOKEN")
            self.assertNotIn("access_token", params)
            self.assertTrue(path.endswith("/messages"))

    def test_network_failure_keeps_committed_page_and_resumes_without_gaps(self):
        self.fail_offset = 2
        with self.assertRaises(AdapterUnavailable) as caught:
            sync_weflow(self.service, self.reader, "group-a@chatroom", 10, 2)
        self.assertNotIn("PRIVATE_TOKEN", str(caught.exception))
        chat = index_chat_id("fixture", "group-a@chatroom")
        saved = self.service.store.get_import_cursor("synthetic", chat)
        self.assertEqual(len(self.service.store.get_messages("synthetic", chat)), 2)
        self.fail_offset = None
        result = sync_weflow(self.service, self.reader, "group-a@chatroom", 10, 2)
        self.assertEqual(result["imported"], 3)
        self.assertNotEqual(self.service.store.get_import_cursor("synthetic", chat), saved)

    def test_redirect_auth_invalid_owner_and_stalled_pages_never_commit(self):
        for mode in ("redirect", "auth", "invalid", "owner", "stalled"):
            self.mode = mode
            with self.assertRaises((AdapterUnavailable, ValueError)) as caught:
                sync_weflow(self.service, self.reader, "group-a@chatroom", 1, 2)
            self.assertNotIn("PRIVATE_TOKEN", str(caught.exception))
            self.assertNotIn("BODY", str(caught.exception))
        self.assertFalse(any("forbidden" in path for path, _, _ in self.requests))
        self.assertEqual(self.service.list_chats(source="local_index")["chats"], [])
        self.assertIsNone(
            self.service.store.get_import_cursor(
                "synthetic", index_chat_id("fixture", "group-a@chatroom")
            )
        )

    def test_message_conflict_and_stale_checkpoint_roll_back_together(self):
        sync_weflow(self.service, self.reader, "group-a@chatroom", 1, 2)
        chat = index_chat_id("fixture", "group-a@chatroom")
        saved = self.service.store.get_import_cursor("synthetic", chat)
        # Simulate a response that reuses a committed ID with different text in the next page.
        self.messages[3] = {**self.messages[3], "platformMessageId": "1", "content": "changed"}
        with self.assertRaises(ValueError):
            sync_weflow(self.service, self.reader, "group-a@chatroom", 1, 2)
        self.assertEqual(self.service.store.get_import_cursor("synthetic", chat), saved)
        self.assertEqual(len(self.service.store.get_messages("synthetic", chat)), 2)
        with self.assertRaisesRegex(ValueError, "checkpoint changed"):
            self.service.store.import_messages("synthetic", [], checkpoint=saved)

    def test_proxy_environment_is_ignored_and_cli_runs_with_isolated_index(self):
        env = {
            **os.environ,
            "WECHAT_PROVIDER_DB": str(self.root / "cli.db"),
            "WECHAT_PROVIDER_ACCOUNT": "cli-synthetic",
            "WECHAT_PROVIDER_READER": "weflow",
            "WECHAT_PROVIDER_WEFLOW_URL": self.reader.base_url,
            "WECHAT_PROVIDER_WEFLOW_TOKEN": "PRIVATE_TOKEN",
            "WECHAT_PROVIDER_WEFLOW_SOURCE_ID": "fixture",
            "WECHAT_PROVIDER_WEFLOW_OWNER_ID": "fixture-owner",
            "http_proxy": "http://127.0.0.1:1",
            "HTTP_PROXY": "http://127.0.0.1:1",
            "no_proxy": "",
            "NO_PROXY": "",
        }
        with contextlib.redirect_stdout(io.StringIO()):
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "wechat_provider.cli",
                    "sync-weflow",
                    "--session-id",
                    "group-a@chatroom",
                    "--page-size",
                    "2",
                ],
                capture_output=True,
                text=True,
                env=env,
                timeout=15,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["imported"], 5)
        self.assertNotIn("PRIVATE_TOKEN", result.stdout + result.stderr)
        self.assertEqual(self.adapter.mock_calls, [])

    def test_invalid_cursor_and_page_bounds_do_not_issue_requests(self):
        for cursor in ("bad-base64", base64.urlsafe_b64encode(b"[]").decode()):
            with self.assertRaises((ValueError, TypeError)):
                self.reader.page("group-a@chatroom", 2, cursor)
        for limit in (0, 101, True):
            with self.assertRaises(ValueError):
                self.reader.page("group-a@chatroom", limit)
        self.assertEqual(self.requests, [])

    @unittest.skipUnless(importlib.util.find_spec("mcp"), "optional MCP SDK not installed")
    def test_real_cli_chatlab_replay_sync_to_mcp_structured_and_indexed_reads(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        env = {
            **os.environ,
            "WECHAT_PROVIDER_DB": str(self.root / "mcp.db"),
            "WECHAT_PROVIDER_ACCOUNT": "mcp-synthetic",
            "WECHAT_PROVIDER_READER": "weflow",
            "WECHAT_PROVIDER_WEFLOW_URL": self.reader.base_url,
            "WECHAT_PROVIDER_WEFLOW_TOKEN": "PRIVATE_TOKEN",
            "WECHAT_PROVIDER_WEFLOW_SOURCE_ID": "fixture",
            "WECHAT_PROVIDER_WEFLOW_OWNER_ID": "fixture-owner",
        }
        path = self.root / "chatlab.json"
        data = document(())
        data["messages"] = self.messages[:2]
        path.write_text(json.dumps(data), encoding="utf-8")
        command = [sys.executable, "-m", "wechat_provider.cli"]
        for expected in (2, 0):
            result = subprocess.run(
                [
                    *command,
                    "import-chatlab",
                    str(path),
                    "--source-id",
                    "fixture",
                    "--session-id",
                    "group-a@chatroom",
                    "--owner-id",
                    "fixture-owner",
                ],
                env=env,
                capture_output=True,
                text=True,
                check=True,
                timeout=15,
            )
            self.assertEqual(json.loads(result.stdout)["imported"], expected)
        result = subprocess.run(
            [*command, "sync-weflow", "--session-id", "group-a@chatroom", "--page-size", "2"],
            env=env,
            capture_output=True,
            text=True,
            check=True,
            timeout=15,
        )
        self.assertEqual(json.loads(result.stdout)["imported"], 3)
        self.assertEqual(json.loads(result.stdout)["duplicates"], 2)

        async def check_mcp():
            params = StdioServerParameters(
                command=sys.executable, args=["-m", "wechat_provider.cli", "serve"], env=env
            )
            async with (
                stdio_client(params) as (reader, writer),
                ClientSession(reader, writer) as session,
            ):
                await session.initialize()

                async def call(name, args):
                    result = await session.call_tool(name, args)
                    self.assertFalse(result.isError)
                    return result.structuredContent or json.loads(result.content[0].text)

                status = await call("wechat_status", {})
                self.assertEqual(status["readers"]["structured_db"]["backend"], "weflow")
                self.assertNotIn("PRIVATE_TOKEN", json.dumps(status))
                chats = await call("wechat_list_chats", {"source": "structured_db"})
                self.assertEqual(chats["chats"][0]["chat_id"], "group-a@chatroom")
                first = await call(
                    "wechat_get_messages",
                    {
                        "source": "structured_db",
                        "chat_id": "group-a@chatroom",
                        "limit": 2,
                    },
                )
                second = await call(
                    "wechat_get_messages",
                    {
                        "source": "structured_db",
                        "chat_id": "group-a@chatroom",
                        "limit": 2,
                        "cursor": first["next_cursor"],
                    },
                )
                self.assertEqual([m["message_id"] for m in second["messages"]], ["3", "4"])
                local = await call("wechat_list_chats", {"source": "local_index"})
                imported = await call(
                    "wechat_get_messages",
                    {
                        "source": "local_index",
                        "chat_id": local["chats"][0]["chat_id"],
                    },
                )
                self.assertEqual(len(imported["messages"]), 5)
                self.assertEqual(imported["id_kind"], "stable_conversation_id")
                search = await call("wechat_search", {"query": "合成合同"})
                self.assertEqual(len(search["matches"]), 5)
                self.assertFalse(search["complete"])

        asyncio.run(check_mcp())
