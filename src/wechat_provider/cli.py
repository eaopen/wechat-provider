"""Minimal local commands; no dynamic shell commands or auto-login."""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

from .models import AdapterUnavailable
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
    chatlab = command.add_parser("import-chatlab", help="Index authorized ChatLab 0.0.2 text")
    chatlab.add_argument("path", type=Path)
    chatlab.add_argument("--source-id", required=True)
    chatlab.add_argument("--session-id", required=True)
    chatlab.add_argument("--owner-id")
    sync = command.add_parser("sync-weflow", help="Explicit bounded read-only WeFlow text pull")
    sync.add_argument("--session-id", required=True)
    sync.add_argument("--max-pages", type=int, default=10)
    sync.add_argument("--page-size", type=int, default=100)
    storage = command.add_parser("storage", help="Storage governance; P0 inventory application")
    storage.add_argument("--state-dir", type=Path)
    operations = storage.add_subparsers(dest="operation", required=True)
    operations.add_parser("discover", help="Find and recommend known macOS WeChat data and backup locations")
    scan = operations.add_parser("scan", help="Inventory explicitly selected directories")
    scan.add_argument("--source", action="append", required=True, help="kind=/absolute/directory")
    scan.add_argument("--max-entries", type=int, default=250000)
    scan.add_argument("--wait", action="store_true")
    files = operations.add_parser("files", help="Browse saved file metadata without rescanning")
    files.add_argument("job_id")
    files.add_argument("--source-id", type=int)
    files.add_argument("--query", default="")
    files.add_argument("--file-kind")
    files.add_argument("--sharing", default="all")
    files.add_argument("--offset", type=int, default=0)
    files.add_argument("--limit", type=int, default=50)
    for name in ("report", "analyze", "cancel", "resume"):
        sub = operations.add_parser(name)
        sub.add_argument("job_id")
        if name == "resume":
            sub.add_argument("--max-entries", type=int, default=250000)
    args = parser.parse_args()
    if args.action == "storage":
        from .service import default_db_path
        from .storage_inventory import TERMINAL, StorageInventory
        manager = StorageInventory(
            args.state_dir or Path(os.environ.get("WECHAT_PROVIDER_STORAGE_DIR",
                                                 str(default_db_path().parent / "storage"))),
            os.environ.get("WECHAT_PROVIDER_ACCOUNT", "default"),
        )
        try:
            if args.operation == "discover":
                from .storage_discovery import discover_sources
                report = discover_sources(reader_state=os.environ.get("WECHAT_PROVIDER_WM_STATE_DIR"))
            elif args.operation == "scan":
                sources = []
                for value in args.source:
                    kind, separator, path = value.partition("=")
                    if not separator:
                        raise ValueError("source must be kind=/absolute/directory")
                    sources.append({"kind": kind, "path": path})
                report = manager.start(sources, args.max_entries)
                if args.wait:
                    try:
                        while report["status"] not in TERMINAL and not report["worker_stale"]:
                            time.sleep(0.2)
                            report = manager.report(report["job_id"])
                    except KeyboardInterrupt:
                        manager.cancel(report["job_id"])
                        raise
            elif args.operation == "resume":
                report = manager.resume(args.job_id, args.max_entries)
            elif args.operation == "analyze":
                from .storage_analysis import analyze_inventory
                report = analyze_inventory(manager, args.job_id)
            elif args.operation == "files":
                from .storage_files import saved_files
                report = saved_files(manager, args.job_id, args.source_id, args.query,
                                     args.file_kind, args.sharing, args.offset, args.limit)
            else:
                report = getattr(manager, args.operation)(args.job_id)
        except (ValueError, OSError):
            parser.error("storage inventory rejected; check selected paths, job ID and private state")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return
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
