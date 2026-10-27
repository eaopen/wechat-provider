import json
import tempfile
import unittest
from pathlib import Path

from wechat_provider.adapters.unsupported import UnsupportedAdapter
from wechat_provider.cli import import_jsonl
from wechat_provider.service import Service
from wechat_provider.store import Store


class ImportTest(unittest.TestCase):
    def test_explicit_jsonl_import(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            path = folder / "import.jsonl"
            path.write_text(
                json.dumps({
                    "chat_id": "项目群", "source_id": "msg-1", "text": "下周开会",
                    "sender": "乙",
                }, ensure_ascii=False) + "\n"
                + json.dumps({"chat_id": "项目群", "text": "missing id"}, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            svc = Service(Store(folder / "index.db"), UnsupportedAdapter("linux"))
            state = import_jsonl(svc, path)
            self.assertEqual(state, {"read": 2, "imported": 1, "skipped": 1})
            self.assertEqual(len(svc.search("开会")["matches"]), 1)


if __name__ == "__main__":
    unittest.main()
