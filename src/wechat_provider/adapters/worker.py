"""Tiny, fixed UI subprocess protocol; not an arbitrary command runner.

Third-party WeChat RPA libraries print chat data to stdout. Executing these
methods in an isolated subprocess avoids breaking the MCP stdio JSON-RPC stream.
"""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Any

from wechat_provider.models import AdapterUnavailable


class WorkerAdapter:
    def __init__(self, platform: str):
        self.platform = platform

    def status(self) -> dict[str, Any]:
        from . import direct_adapter
        return direct_adapter(self.platform).status()

    def _call(self, action: str, **payload: Any) -> Any:
        request = {"action": action, "args": payload}
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "wechat_provider.adapters.worker_main"],
                input=json.dumps(request, ensure_ascii=False),
                text=True,
                capture_output=True,
                check=False,
                timeout=120,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("Desktop UI operation timed out; outcome uncertain") from exc
        try:
            response = json.loads(proc.stdout)
        except (ValueError, TypeError) as exc:
            raise RuntimeError("Invalid local UI worker response") from exc
        if proc.returncode or not response.get("ok"):
            kind = response.get("kind", "RuntimeError")
            detail = response.get("detail") or kind
            if kind == "AdapterUnavailable":
                raise AdapterUnavailable(detail)
            raise RuntimeError(kind)
        return response.get("result")

    def list_chats(self, limit: int) -> list[dict[str, Any]]:
        return self._call("list_chats", limit=limit)

    def get_messages(self, chat_id: str, limit: int) -> list[dict[str, Any]]:
        return self._call("get_messages", chat_id=chat_id, limit=limit)

    def send_text(self, chat_id: str, text: str) -> None:
        self._call("send_text", chat_id=chat_id, text=text)
