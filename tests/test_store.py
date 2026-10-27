import tempfile
import unittest
from pathlib import Path

from wechat_provider.store import Store


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "wechat.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_message_dedup_search_and_account_isolation(self):
        row = {
            "source_id": "m-01", "text": "下周一验收微信助手",
            "sender": "测试", "sent_at": "2026-10-27T10:00:00+08:00",
            "source": "jsonl",
        }
        self.assertEqual(self.store.add_messages("a", "测试群", [row, row]), 1)
        self.assertEqual(len(self.store.get_messages("a", "测试群")), 1)
        self.assertEqual(len(self.store.search("a", "周一")), 1)
        self.assertEqual(len(self.store.search("a", "微信助手", chat_id="测试群")), 1)
        self.assertEqual(self.store.search("b", "微信助手"), [])
        self.assertEqual(self.store.search("a", "微信助手", chat_id="其他群"), [])

    def test_send_idempotency_and_state(self):
        first = self.store.queue_send("a", "文件传输助手", "ping", "once")
        again = self.store.queue_send("a", "文件传输助手", "ping", "once")
        self.assertEqual(first["task_id"], again["task_id"])
        with self.assertRaises(ValueError):
            self.store.queue_send("a", "其他人", "ping", "once")
        self.assertTrue(self.store.claim(first["task_id"]))
        self.assertFalse(self.store.claim(first["task_id"]))
        self.store.finish(first["task_id"], "submitted", "UI completed")
        self.assertEqual(
            self.store.get_task(first["task_id"])["status"], "submitted"
        )

    def test_no_stable_source_id_no_index(self):
        self.assertEqual(self.store.add_messages("a", "群", [{"text": "消息"}]), 0)


if __name__ == "__main__":
    unittest.main()
