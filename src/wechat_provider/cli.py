"""Minimal local commands; no dynamic shell commands or auto-login."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .native_export import mint_grant
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
    daemon = command.add_parser("daemon", help="Independent same-user Unix socket service")
    daemon.add_argument("--socket", required=True)
    proxy = command.add_parser("proxy", help="MCP stdio proxy; never starts a reader")
    proxy.add_argument("--socket", required=True)
    imp = command.add_parser("import-jsonl", help="Index authorized normalized messages")
    imp.add_argument("path", type=Path)
    grant = command.add_parser(
        "grant-native-export",
        help="Test/acceptance: mint a one-time native-export grant (product path is Companion)",
    )
    grant.add_argument("path", type=Path)
    grant.add_argument("--purpose", choices=("preview", "save"), required=True)
    grant.add_argument("--grant-dir", type=Path, required=True)
    preview = command.add_parser(
        "preview-native-export", help="Bounded preview of a granted merged-forward ZIP")
    preview.add_argument("grant_token")
    preview.add_argument("--timezone-offset-minutes", type=int, default=None)
    preview.add_argument("--offset", type=int, default=0)
    preview.add_argument("--limit", type=int, default=200)
    save = command.add_parser(
        "save-native-export", help="Import a previewed merged-forward ZIP into the local index")
    save.add_argument("grant_token")
    save.add_argument("--fingerprint", required=True)
    save.add_argument("--timezone-offset-minutes", type=int, default=None)
    args = parser.parse_args()
    if args.action == "daemon":
        from .local_ipc import serve
        serve(args.socket, new_service())
        return
    if args.action == "proxy":
        from . import mcp_server
        from .local_ipc import RemoteService
        mcp_server._service = RemoteService(args.socket)
        mcp_server.main()
        return
    if args.action == "serve":
        try:
            from .mcp_server import main as run_mcp
        except ImportError as exc:
            parser.error("MCP SDK missing: install package with 'pip install -e .'")
            raise AssertionError from exc
        run_mcp()
        return
    if args.action == "grant-native-export":
        try:
            result = mint_grant(args.grant_dir, args.path, args.purpose)
        except ValueError as exc:
            parser.error(str(exc))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    svc = new_service()
    if args.action == "status":
        print(json.dumps(svc.status(), ensure_ascii=False, indent=2))
    elif args.action == "preview-native-export":
        try:
            result = svc.preview_native_export(
                args.grant_token, args.timezone_offset_minutes, args.offset, args.limit)
        except ValueError as exc:
            parser.error(str(exc))
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.action == "save-native-export":
        try:
            result = svc.save_native_export(
                args.grant_token, args.fingerprint, args.timezone_offset_minutes)
        except ValueError as exc:
            parser.error(str(exc))
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        try:
            result = import_jsonl(svc, args.path)
        except ValueError as exc:
            parser.error(str(exc))
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
