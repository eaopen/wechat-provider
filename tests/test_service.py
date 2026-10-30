import tempfile
import unittest
from pathlib import Path

from wechat_provider.models import AdapterUnavailable
from wechat_provider.service import Service
from wechat_provider.store import Store


class FakeAdapter:
    def __init__(self, fail=False, unavailable=False):
        self.sends = []
        self.fail = fail
        self.unavailable = unavailable

    def status(self):
        return {"ready_for_attempt": True, "platform": "fake"}

    def list_chats(self, limit):
        return [{"chat_id": "文件传输助手", "title": "文件传输助手"}][:limit]

    def get_messages(self, chat_id, limit):
        return [{"text": "下周一给你材料", "sender": "甲", "sent_at": None}][-limit:]

    def send_text(self, chat_id, text):
        if self.unavailable:
            raise AdapterUnavailable("not installed")
        self.sends.append((chat_id, text))
        if self.fail:
            raise RuntimeError("UI state unknown")


class ServiceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "messages.db")
        self.backend = FakeAdapter()
        self.service = Service(self.store, self.backend, account_key="userA")

    def tearDown(self):
        self.tmp.cleanup()

    def test_read_indexes_and_searches_only_visible_text(self):
        self.assertFalse(self.service.list_chats()["complete"])
        msg = self.service.get_messages("文件传输助手")
        self.assertEqual(len(msg["messages"]), 1)
        self.assertFalse(msg["complete"])
        self.assertEqual(len(self.service.search("下周一")["matches"]), 1)
        # Repeat UI read with same order and contents should not duplicate.
        self.service.get_messages("文件传输助手")
        self.assertEqual(len(self.store.get_messages("userA", "文件传输助手")), 1)

    def test_send_is_submitted_not_verified_and_idempotent(self):
        one = self.service.send_message("文件传输助手", "测试文本", "send-once")
        again = self.service.send_message("文件传输助手", "测试文本", "send-once")
        self.assertEqual(one["task_id"], again["task_id"])
        self.assertEqual(one["status"], "submitted")
        self.assertEqual(self.backend.sends, [("文件传输助手", "测试文本")])
        self.assertEqual(self.service.get_send_status(one["task_id"])["status"], "submitted")

    def test_failed_ui_attempt_is_uncertain_without_resend(self):
        failing = FakeAdapter(fail=True)
        service = Service(self.store, failing, "userA")
        one = service.send_message("文件传输助手", "测试", "fail-once")
        self.assertEqual(one["status"], "uncertain")
        service.send_message("文件传输助手", "测试", "fail-once")
        self.assertEqual(len(failing.sends), 1)

    def test_missing_backend_is_failed_before_ui(self):
        service = Service(self.store, FakeAdapter(unavailable=True), "userA")
        state = service.send_message("文件传输助手", "测试", "missing")
        self.assertEqual(state["status"], "failed")

    def test_bad_send_rejected_before_queuing(self):
        for chat, content in [("", "x"), ("test", ""), ("test", "x" * 2001)]:
            with self.assertRaises(ValueError):
                self.service.send_message(chat, content)

    def test_explicit_index_reads_are_account_scoped_and_never_touch_adapter(self):
        from unittest.mock import Mock
        self.store.add_messages("userA", "synthetic", [{"source_id": "1", "text": "hello", "source": "jsonl"}])
        self.store.add_messages("userB", "private", [{"source_id": "2", "text": "other", "source": "jsonl"}])
        adapter = Mock()
        service = Service(self.store, adapter, "userA")
        chats = service.list_chats(source="local_index")
        self.assertEqual([x["chat_id"] for x in chats["chats"]], ["synthetic"])
        result = service.get_messages("synthetic", source="local_index")
        self.assertFalse(result["live_read"])
        self.assertEqual(result["messages"][0]["source"], "jsonl")
        self.assertIn("imported_at", result["messages"][0])
        self.assertEqual(service.get_messages("private", source="local_index")["messages"], [])
        self.assertEqual(adapter.mock_calls, [])
        with self.assertRaises(ValueError):
            service.get_messages("synthetic", source="invalid")


if __name__ == "__main__":
    unittest.main()
