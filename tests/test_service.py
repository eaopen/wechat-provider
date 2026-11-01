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
        if self.unavailable:
            raise AdapterUnavailable("not installed")
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

    def test_user_selected_excerpt_is_opt_in_idempotent_and_account_scoped(self):
        from unittest.mock import Mock
        read_only = Service(self.store, Mock(), "userA")
        first = read_only.save_excerpt("测试好友", "甲：明天出差")
        self.assertEqual(first["imported"], 1)
        self.assertFalse(first["live_read"])
        second = read_only.save_excerpt("测试好友", "甲：明天出差")
        self.assertEqual(second["imported"], 0)
        self.assertEqual(second["duplicates"], 1)
        self.assertEqual(len(read_only.list_chats(source="local_index")["chats"]), 1)
        self.assertEqual(read_only.get_messages("测试好友", source="local_index")["messages"][0]["source"], "manual_excerpt")
        self.assertEqual(Service(self.store, Mock(), "userB").list_chats(source="local_index")["chats"], [])
        # Real NUL bytes must be rejected (not the two-character escape).
        for title, body in (("", "x"), ("name", ""), ("name", "x" * 7801),
                            ("x" * 201, "ok"), ("bad\0label", "ok"),
                            ("name", "bad\0body")):
            with self.assertRaises(ValueError):
                read_only.save_excerpt(title, body)
        with self.assertRaises(ValueError):
            read_only.forget_saved_excerpts("bad\0label")
        self.assertEqual(read_only.forget_saved_excerpts("测试好友")["removed"], 1)
        self.assertEqual(read_only.forget_saved_excerpts("测试好友")["removed"], 0)
        self.assertEqual(read_only.list_chats(source="local_index")["chats"], [])
        self.assertEqual(read_only.adapter.mock_calls, [])

    def test_excerpt_byte_boundaries_are_inclusive(self):
        from unittest.mock import Mock
        service = Service(self.store, Mock(), "userB")
        # Exact 200-byte label and 7800-byte body stay inside the documented bound.
        first = service.save_excerpt("x" * 200, "字" * 2600)  # 7800 UTF-8 bytes
        self.assertEqual((first["imported"], first["duplicates"]), (1, 0))
        self.assertEqual(service.save_excerpt("x" * 200, "字" * 2600)["duplicates"], 1)
        self.assertEqual(service.forget_saved_excerpts("x" * 200)["removed"], 1)

    def test_local_forget_preserves_non_manual_imports(self):
        self.store.add_messages("userA", "项目群", [
            {"source_id":"existing", "text":"独立导入", "source":"jsonl"}])
        self.service.save_excerpt("项目群", "我：手动选中的片段")
        self.assertEqual(self.service.forget_saved_excerpts("项目群")["removed"], 1)
        remaining = self.service.get_messages("项目群",source="local_index")["messages"]
        self.assertEqual(len(remaining),1)
        self.assertEqual(remaining[0]["source"],"jsonl")

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

    def test_index_list_reports_honest_provenance(self):
        from unittest.mock import Mock
        adapter = Mock()
        service = Service(self.store, adapter, "userA")
        service.save_excerpt("只保存过片段", "我：手动保存")
        self.store.add_messages("userA", "混合来源", [{"source_id": "i1", "text": "导入", "source": "jsonl"}])
        service.save_excerpt("混合来源", "我：也保存过")
        rows = {x["chat_id"]: x for x in service.list_chats(source="local_index")["chats"]}
        self.assertTrue(rows["只保存过片段"]["manual_excerpt_only"])
        self.assertFalse(rows["混合来源"]["manual_excerpt_only"])
        self.assertEqual(rows["混合来源"]["message_count"], 2)
        self.assertEqual(adapter.mock_calls, [])

    def test_unavailable_desktop_reader_is_an_error_not_an_empty_list(self):
        service = Service(self.store, FakeAdapter(unavailable=True), "userA")
        with self.assertRaises(AdapterUnavailable):
            service.list_chats(source="desktop_ui")
        # Index reads never depend on the desktop reader.
        service.save_excerpt("本机标签", "我：只用本机索引")
        self.assertEqual([x["chat_id"] for x in service.list_chats(source="local_index")["chats"]],
                         ["本机标签"])


if __name__ == "__main__":
    unittest.main()
