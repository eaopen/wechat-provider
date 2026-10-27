import sys
import types
import unittest
from unittest.mock import patch

from wechat_provider.adapters.macos import MacAdapter
from wechat_provider.adapters.windows import WindowsAdapter


class AdapterIntegrationShapesTest(unittest.TestCase):
    def test_windows_uses_upstream_pyweixin_methods(self):
        calls = []

        class Messages:
            @staticmethod
            def dump_recent_sessions(**kwargs):
                calls.append(("sessions", kwargs))
                return [("文件传输助手", "12:00", "hello")]

            @staticmethod
            def pull_messages(**kwargs):
                calls.append(("read", kwargs))
                return [{"消息内容": "hi", "消息发送人": "甲"}]

            @staticmethod
            def send_messages_to_friend(**kwargs):
                calls.append(("send", kwargs))

        with patch.dict(sys.modules, {"pyweixin": types.SimpleNamespace(Messages=Messages)}):
            a = WindowsAdapter()
            self.assertEqual(a.list_chats(10)[0]["chat_id"], "文件传输助手")
            self.assertEqual(a.get_messages("文件传输助手", 2)[0]["text"], "hi")
            a.send_text("文件传输助手", "测试")
        self.assertEqual(calls[-1][1]["messages"], ["测试"])
        self.assertEqual(calls[-1][1]["close_weixin"], False)

    def test_macos_uses_upstream_wxauto_methods(self):
        calls = []

        class WeChat:
            def GetSessionList(self):
                return [{"name": "文件传输助手", "last_message": "ok", "unread_count": 0}]

            def GetChatHistory(self, friend):
                calls.append(("read", friend))
                return ["A", "B"]

            def SendToFriend(self, friend, text):
                calls.append(("send", friend, text))
                return True

        with patch.dict(sys.modules, {"wxauto_mac": types.SimpleNamespace(WeChat=WeChat)}):
            a = MacAdapter()
            self.assertEqual(a.list_chats(2)[0]["chat_id"], "文件传输助手")
            self.assertEqual(a.get_messages("文件传输助手", 1)[0]["text"], "B")
            a.send_text("文件传输助手", "hello")
        self.assertEqual(calls[-1], ("send", "文件传输助手", "hello"))


if __name__ == "__main__":
    unittest.main()
