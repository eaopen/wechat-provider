"""Minimal local commands; no dynamic shell commands or auto-login."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .service import new_service


def import_jsonl(service, path: Path) -> dict:
    """Import normalized, user-provided JSONL, never raw encrypted WeChat DB."""
    counts = {"read": 0, "imported": 0, "skipped": 0}
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            counts["read"] += 1
            obj = json.loads(line)
            chat_id = str(obj.get("chat_id") or "").strip()
            source_id = str(obj.get("source_id") or "").strip()
            content = str(obj.get("text") or "")
            if not chat_id or not source_id or not content:
                counts["skipped"] += 1
                continue
            counts["imported"] += service.store.add_messages(
                service.account_key, chat_id, [{
                    "source_id": source_id,
                    "text": content,
                    "sender": obj.get("sender"),
                    "sent_at": obj.get("sent_at"),
                    "source": obj.get("source") or "jsonl",
                }]
            )
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="wechat-provider",
        description="Local-first WeChat MCP provider for macOS and Windows",
    )
    command = parser.add_subparsers(dest="action", required=True)
    command.add_parser("status", help="Offline prerequisites and local DB state")
    command.add_parser("serve", help="Start local stdio MCP server")
    imp = command.add_parser("import-jsonl", help="Index authorized normalized messages")
    imp.add_argument("path", type=Path)
    args = parser.parse_args()
    if args.action == "serve":
        try:
            from .mcp_server import main as run_mcp
        except ImportError as exc:
            parser.error("MCP SDK missing: install package with 'pip install -e .'")
            raise AssertionError from exc
        run_mcp()
        return
    svc = new_service()
    if args.action == "status":
        print(json.dumps(svc.status(), ensure_ascii=False, indent=2))
    else:
        print(json.dumps(import_jsonl(svc, args.path), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
