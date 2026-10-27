import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from wechat_provider.adapters import native_adapter
from wechat_provider.adapters.worker import WorkerAdapter
from wechat_provider.service import Service
from wechat_provider.store import Store


class WorkerTest(unittest.TestCase):
    def test_platform_selects_isolated_backend(self):
        self.assertIsInstance(native_adapter("darwin"), WorkerAdapter)
        self.assertIsInstance(native_adapter("win32"), WorkerAdapter)

    def test_protocol_sends_only_fixed_module_with_json_payload(self):
        requests = []

        def fake_run(cmd, **kwargs):
            requests.append((cmd, json.loads(kwargs["input"])))
            return subprocess.CompletedProcess(cmd, 0, '{"ok": true, "result": null}', "")

        with patch("wechat_provider.adapters.worker.subprocess.run", side_effect=fake_run):
            a = WorkerAdapter("darwin")
            a.send_text("文件传输助手", "hello")
        self.assertEqual(requests[0][0][1:], ["-m", "wechat_provider.adapters.worker_main"])
        self.assertEqual(requests[0][1]["args"]["chat_id"], "文件传输助手")

    def test_worker_failure_is_uncertain_for_send(self):
        with tempfile.TemporaryDirectory() as temp:
            service = Service(
                Store(Path(temp) / "state.db"), WorkerAdapter("darwin")
            )
            result = subprocess.CompletedProcess(
                [], 1, '{"ok": false, "kind": "RuntimeError"}', ""
            )
            with patch("wechat_provider.adapters.worker.subprocess.run", return_value=result):
                state = service.send_message("文件传输助手", "text", "e-1")
            self.assertEqual(state["status"], "uncertain")


if __name__ == "__main__":
    unittest.main()
