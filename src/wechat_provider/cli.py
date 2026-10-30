"""Minimal local commands; no dynamic shell commands or auto-login."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .service import new_service


def import_jsonl(service, path: Path) -> dict:
    """Validate a bounded authorized export, then atomically import it."""
    from .structured_import import read_jsonl

    rows, counts = read_jsonl(path)
    result = service.store.import_messages(service.account_key, rows)
    return {**counts, **result}


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
        try:
            result = import_jsonl(svc, args.path)
        except ValueError as exc:
            parser.error(str(exc))
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
