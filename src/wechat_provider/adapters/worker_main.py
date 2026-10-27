"""Executed only via WorkerAdapter's fixed Python module command."""

from __future__ import annotations

import contextlib
import json
import os
import sys

from wechat_provider.adapters import direct_adapter
from wechat_provider.models import AdapterUnavailable


def main() -> None:
    try:
        request = json.loads(sys.stdin.read())
        action = request.get("action")
        args = request.get("args") or {}
        if action not in {"list_chats", "get_messages", "send_text"}:
            raise ValueError("Unsupported worker action")
        if not isinstance(args, dict):
            raise ValueError("Invalid worker arguments")

        # Suppress both Python prints and fd=1 writes by dependencies.
        original_stdout = os.dup(sys.stdout.fileno())
        try:
            with open(os.devnull, "w", encoding="utf-8") as sink:
                sys.stdout.flush()
                os.dup2(sink.fileno(), sys.stdout.fileno())
                try:
                    with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
                        adapter = direct_adapter()
                        if action == "list_chats":
                            result = adapter.list_chats(int(args["limit"]))
                        elif action == "get_messages":
                            result = adapter.get_messages(
                                str(args["chat_id"]), int(args["limit"])
                            )
                        else:
                            result = adapter.send_text(
                                str(args["chat_id"]), str(args["text"])
                            )
                finally:
                    sys.stdout.flush()
                    os.dup2(original_stdout, sys.stdout.fileno())
        finally:
            os.close(original_stdout)
        output = {"ok": True, "result": result}
    except AdapterUnavailable as exc:
        output = {"ok": False, "kind": "AdapterUnavailable", "detail": str(exc)}
    except Exception as exc:
        # Never return exception str here: GUI libraries may include chat content.
        output = {"ok": False, "kind": type(exc).__name__}
    sys.stdout.write(json.dumps(output, ensure_ascii=False) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
