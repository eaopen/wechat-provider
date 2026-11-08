"""Minimal local commands; no dynamic shell commands or auto-login."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .models import AdapterUnavailable
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
    chatlab = command.add_parser("import-chatlab", help="Index authorized ChatLab 0.0.2 text")
    chatlab.add_argument("path", type=Path)
    chatlab.add_argument("--source-id", required=True)
    chatlab.add_argument("--session-id", required=True)
    chatlab.add_argument("--owner-id")
    sync = command.add_parser("sync-weflow", help="Explicit bounded read-only WeFlow text pull")
    sync.add_argument("--session-id", required=True)
    sync.add_argument("--max-pages", type=int, default=10)
    sync.add_argument("--page-size", type=int, default=100)
    args = parser.parse_args()
    if args.action == "daemon":
        from .local_ipc import serve
        serve(args.socket, new_service())
        return
    if args.action == "proxy":
        from .local_ipc import RemoteService
        from . import mcp_server
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
    svc = new_service()
    if args.action == "status":
        print(json.dumps(svc.status(), ensure_ascii=False, indent=2))
    else:
        try:
            if args.action == "import-chatlab":
                from .chatlab_import import import_chatlab
                result = import_chatlab(svc, args.path, args.source_id, args.session_id, args.owner_id)
            elif args.action == "sync-weflow":
                from .weflow_reader import WeFlowReader, sync_weflow
                result = sync_weflow(svc, WeFlowReader.from_env(), args.session_id,
                                     args.max_pages, args.page_size)
            else:
                result = import_jsonl(svc, args.path)
        except ValueError as exc:
            parser.error(str(exc))
        except OSError:
            parser.error("local export or index unavailable")
        except AdapterUnavailable as exc:
            parser.error(str(exc))
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
