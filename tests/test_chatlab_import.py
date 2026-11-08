import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from wechat_provider.chatlab_import import import_chatlab, index_chat_id
from wechat_provider.service import Service
from wechat_provider.store import Store


def document(ids=("1", "2"), session="group-a@chatroom", owner="fixture-owner"):
    return {
        "chatlab": {"version": "0.0.2"},
        "meta": {"name": "虚构同名会话", "platform": "im", "type": "group",
                 "groupId": session, "ownerId": owner},
        "members": [{"platformId": "fixture-sender", "accountName": "虚构联系人"}],
        "messages": [{"platformMessageId": message_id, "sender": "fixture-sender",
                      "timestamp": 1794000000, "type": 0, "content": "合成合同 " + message_id}
                     for message_id in ids],
    }


class ChatLabImportTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "page.json"
        self.adapter = Mock()
        self.service = Service(Store(self.root / "index.db"), self.adapter, "synthetic")

    def load(self, data, source="fixture", session="group-a@chatroom"):
        self.path.write_text(json.dumps(data), encoding="utf-8")
        return import_chatlab(self.service, self.path, source, session, "fixture-owner")

    def test_time_replay_same_names_source_account_and_search(self):
        self.assertEqual(self.load(document())["imported"], 2)
        self.assertEqual(self.load(document())["duplicates"], 2)
        self.load(document(session="group-b@chatroom"), session="group-b@chatroom")
        self.load(document(), source="other-source")
        chats = self.service.list_chats(source="local_index")["chats"]
        self.assertEqual(len(chats), 3)
        self.assertEqual({c["title"] for c in chats}, {"虚构同名会话"})
        self.assertEqual(len({c["chat_id"] for c in chats}), 3)
        chat = index_chat_id("fixture", "group-a@chatroom")
        reply = self.service.get_messages(chat, source="local_index")
        self.assertEqual(reply["id_kind"], "stable_conversation_id")
        self.assertEqual(reply["messages"][0]["sent_at"], "2026-11-06T21:20:00+00:00")
        self.assertEqual(reply["messages"][0]["sender"], "fixture-sender")
        self.assertEqual(len(self.service.search("合成合同")["matches"]), 6)
        self.assertEqual(self.service.store.list_chats("another-account"), [])
        with self.assertRaisesRegex(ValueError, "read-only"):
            self.service.send_message(chat, "must not reach UI")
        self.assertEqual(self.adapter.mock_calls, [])

    def test_jsonl_page_documents_import_atomically_and_non_text_is_counted(self):
        self.path = self.root / "pages.jsonl"
        first, second = document(("1",)), document(("2",))
        second["messages"].append({"type": 1})
        self.path.write_text(json.dumps(first) + "\n" + json.dumps(second), encoding="utf-8")
        result = import_chatlab(self.service, self.path, "fixture", "group-a@chatroom")
        self.assertEqual((result["read"], result["imported"], result["skipped_non_text"]), (3, 2, 1))
        self.assertFalse(result["complete"])

    def test_conflict_rolls_back_text_fts_metadata_and_cursor(self):
        self.load(document(("1",)))
        changed = document(("new", "1"))
        changed["messages"][1]["content"] = "PRIVATE_BODY_DO_NOT_ECHO"
        changed["meta"]["name"] = "changed-title"
        with self.assertRaises(ValueError) as caught:
            self.load(changed)
        self.assertNotIn("PRIVATE_BODY", str(caught.exception))
        self.assertEqual(self.service.search("合成合同 new")["matches"], [])
        self.assertEqual(self.service.list_chats(source="local_index")["chats"][0]["title"],
                         "虚构同名会话")

    def test_identity_missing_id_types_and_schema_fail_closed(self):
        changes = [
            lambda d: d["meta"].update(ownerId="wrong-owner"),
            lambda d: d["meta"].update(groupId="wrong-group"),
            lambda d: d["chatlab"].update(version="future"),
            lambda d: d["messages"][0].pop("platformMessageId"),
            lambda d: d["messages"][0].update(timestamp=True),
            lambda d: d["messages"][0].update(content="a" * 32001),
            lambda d: d["messages"][0].update(sender="bad\0id"),
        ]
        for mutate in changes:
            data = copy.deepcopy(document())
            mutate(data)
            with self.assertRaises(ValueError):
                self.load(data)
        self.assertEqual(self.service.list_chats(source="local_index")["chats"], [])

    def test_private_chat_id_is_explicit_and_owner_cannot_switch(self):
        data = document()
        data["meta"].update(type="private")
        data["meta"].pop("groupId")
        self.load(data, session="private-a")
        data["meta"]["ownerId"] = "other-owner"
        self.path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(ValueError):
            import_chatlab(self.service, self.path, "fixture", "private-a")
