"""Issue #9 permission-boundary tests for the independent Provider IPC.

Synthetic fixtures only: no real WeChat database, no TCC/FDA access, no
network, no message content. The cases assert that the same-user Unix-socket
boundary stays honest:

* ``status.ipc.read_access`` is ``not_checked`` until a structured read really
  succeeded, ``verified`` afterwards, ``access_denied`` after a denied read,
  and ``tcc_verified`` is always false (no TCC/FDA claim is ever inferred).
* ``status`` is side-effect free: it never opens the reader or a WeChat source.
* the served socket directory is 0700 and the socket 0600; group/world
  accessible parents, symlink ancestors and mismatched peer UIDs are rejected.
* error codes crossing IPC stay in a small stable, non-reflective set.
* the no-FDA fallback (``WECHAT_PROVIDER_READER=unconfigured``) serves
  ``local_index`` reads and fails closed for ``structured_db`` instead of
  touching WeChat data or pretending a permission state.

Unix-socket cases skip on Windows; the MCP proxy case also skips when the
optional ``mcp`` package is not installed.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import wechat_provider
from wechat_provider.local_ipc import (
    MAX_FRAME,
    RemoteService,
    handle,
    receive,
    send,
    serve,
    socket_path,
)
from wechat_provider.models import AdapterUnavailable
from wechat_provider.native_export import ERROR_GRANT_INVALID
from wechat_provider.service import new_service

MCP_AVAILABLE = importlib.util.find_spec("mcp") is not None
POSIX_ONLY = unittest.skipUnless(os.name == "posix", "Unix sockets require POSIX")

# The only error codes allowed to cross the same-user IPC boundary.
ALLOWED_IPC_ERRORS = frozenset({
    "invalid_argument",
    "source_access_denied",
    "reader_unavailable",
    "provider_ipc_failed",
    ERROR_GRANT_INVALID,
})


def _temp_dir(case: unittest.TestCase) -> Path:
    """A private 0700 temp dir with symlinks resolved (macOS /var -> /private/var)."""
    root = Path(tempfile.mkdtemp(prefix="wc-ipc-boundary-")).resolve()
    case.addCleanup(shutil.rmtree, root, True)
    return root


def _exchange(service, request, uid=None):
    """Run one real ``handle`` exchange over a socketpair and return the response.

    ``request=None`` skips the write so a rejected peer cannot race the error
    frame (the server rejects before reading the request in that case).
    """
    server, client = socket.socketpair()
    def run_server():
        with server:
            if uid is None:
                handle(server, service)
            else:
                with patch("wechat_provider.local_ipc.peer_uid", return_value=uid):
                    handle(server, service)
    thread = threading.Thread(target=run_server, daemon=True)
    thread.start()
    response = None
    try:
        with client:
            if request is not None:
                try:
                    send(client, request)
                except OSError:
                    pass  # rejected peers may be disconnected before the write
            response = receive(client)
    finally:
        thread.join(5)
    if thread.is_alive():
        raise AssertionError("IPC handler thread did not finish")
    return response


def _raw_exchange(service, frame: bytes):
    """Feed pre-encoded frame bytes through a real ``handle`` and return the response."""
    server, client = socket.socketpair()
    def run_server():
        with server:
            handle(server, service)
    thread = threading.Thread(target=run_server, daemon=True)
    thread.start()
    def write():
        try:
            client.sendall(frame)
        except OSError:
            pass  # the server may reject an oversized frame and close early
    writer = threading.Thread(target=write, daemon=True)
    writer.start()
    try:
        return receive(client)
    finally:
        client.close()
        writer.join(5)
        thread.join(5)


class StatusOnlyService:
    """Status is side-effect free; every read method records a test failure."""

    def __init__(self):
        self.status_calls = 0
        self.read_calls = []

    def status(self):
        self.status_calls += 1
        return {
            "provider": "wechat-provider",
            "readers": {
                "local_index": {"available": True},
                "structured_db": {"available": False, "configured": False},
            },
            "capabilities": {"local_index_read": True, "structured_db_read": False},
        }

    def get_messages(self, chat_id, limit=50, source="local_index"):
        self.read_calls.append(source)
        raise AssertionError("status must not open a reader or database")

    def list_chats(self, limit=30, source="desktop_ui", cursor=None):
        self.read_calls.append(source)
        raise AssertionError("status must not open a reader or database")


class LocalIndexReadService(StatusOnlyService):
    def get_messages(self, chat_id, limit=50, source="local_index"):
        self.read_calls.append(source)
        return {"messages": [], "chat_id": chat_id, "source": "local_index"}


class StructuredReadService(StatusOnlyService):
    def get_messages(self, chat_id, limit=50, source="local_index"):
        self.read_calls.append(source)
        return {"messages": [], "chat_id": chat_id, "source": "structured_db"}

    def list_chats(self, limit=30, source="desktop_ui", cursor=None):
        self.read_calls.append(source)
        # Only the result carries the structured_db marker (not the request).
        return {"chats": [], "source": "structured_db"}


class DeniedReadService(StatusOnlyService):
    def __init__(self, detail="source_access_denied"):
        super().__init__()
        self.detail = detail
        self.denied = True

    def get_messages(self, chat_id, limit=50, source="local_index"):
        self.read_calls.append(source)
        if self.denied:
            raise AdapterUnavailable(self.detail)
        return {"messages": [], "chat_id": chat_id, "source": "structured_db"}


class ReadAccessStateTest(unittest.TestCase):
    """status.ipc.read_access state machine + permanent tcc_verified=false."""

    def _ipc(self, response):
        self.assertTrue(response["ok"], response)
        return response["result"]["ipc"]

    def test_status_before_any_read_is_not_checked_and_side_effect_free(self):
        service = StatusOnlyService()
        request = {"version": 1, "method": "status"}
        first = _exchange(service, request)
        second = _exchange(service, request)
        for response in (first, second):
            ipc = self._ipc(response)
            self.assertEqual(ipc["read_access"], "not_checked")
            self.assertIs(ipc["tcc_verified"], False)
            self.assertEqual(ipc["permission_owner"], "independent_provider")
            self.assertEqual(ipc["transport"], "unix")
            self.assertIs(ipc["same_user"], True)
            self.assertEqual(response["result"]["readers"]["local_index"],
                             {"available": True})
        # status twice and still no reader/database call
        self.assertEqual(service.status_calls, 2)
        self.assertEqual(service.read_calls, [])

    def test_successful_structured_read_sets_verified(self):
        service = StructuredReadService()
        read = _exchange(service, {"version": 1, "method": "get_messages",
                                   "args": ["conversation-id"],
                                   "kwargs": {"source": "structured_db"}})
        self.assertTrue(read["ok"], read)
        self.assertEqual(read["result"]["source"], "structured_db")
        self.assertEqual(service.read_calls, ["structured_db"])
        ipc = self._ipc(_exchange(service, {"version": 1, "method": "status"}))
        self.assertEqual(ipc["read_access"], "verified")
        self.assertIs(ipc["tcc_verified"], False)

    def test_structured_result_source_alone_sets_verified(self):
        service = StructuredReadService()
        read = _exchange(service, {"version": 1, "method": "list_chats",
                                   "args": [5]})
        self.assertTrue(read["ok"], read)
        self.assertEqual(read["result"]["source"], "structured_db")
        ipc = self._ipc(_exchange(service, {"version": 1, "method": "status"}))
        self.assertEqual(ipc["read_access"], "verified")

    def test_local_index_read_does_not_claim_verified(self):
        service = LocalIndexReadService()
        read = _exchange(service, {"version": 1, "method": "get_messages",
                                   "args": ["chat"], "kwargs": {"source": "local_index"}})
        self.assertTrue(read["ok"], read)
        self.assertEqual(read["result"]["source"], "local_index")
        ipc = self._ipc(_exchange(service, {"version": 1, "method": "status"}))
        # A local-index read is not evidence of structured/WeChat DB access.
        self.assertEqual(ipc["read_access"], "not_checked")
        self.assertIs(ipc["tcc_verified"], False)

    def test_source_access_denied_sets_access_denied_without_leaking(self):
        private = "/Users/synthetic/Library/Containers/com.tencent.xinWeChat/private.db"
        service = DeniedReadService("source_access_denied: " + private)
        denied = _exchange(service, {"version": 1, "method": "get_messages",
                                     "args": ["chat"],
                                     "kwargs": {"source": "structured_db"}})
        self.assertEqual(denied, {"ok": False, "error": "source_access_denied"})
        serialized = json.dumps(denied)
        self.assertNotIn(private, serialized)
        self.assertNotIn("com.tencent", serialized)
        ipc = self._ipc(_exchange(service, {"version": 1, "method": "status"}))
        self.assertEqual(ipc["read_access"], "access_denied")
        self.assertIs(ipc["tcc_verified"], False)
        # Last observation wins: a later successful structured read is verified.
        service.denied = False
        read = _exchange(service, {"version": 1, "method": "get_messages",
                                   "args": ["chat"],
                                   "kwargs": {"source": "structured_db"}})
        self.assertTrue(read["ok"], read)
        ipc = self._ipc(_exchange(service, {"version": 1, "method": "status"}))
        self.assertEqual(ipc["read_access"], "verified")
        self.assertIs(ipc["tcc_verified"], False)


class RealServiceStatusSideEffectTest(unittest.TestCase):
    """The real Service.status must not query the reader (no WeChat/TCC access)."""

    def test_real_service_status_never_queries_reader(self):
        root = _temp_dir(self)
        env = {"WECHAT_PROVIDER_DB": str(root / "index.db"),
               "WECHAT_PROVIDER_READER": "unconfigured"}
        with patch.dict(os.environ, env):
            service = new_service()
        class ReaderSpy:
            def __init__(self):
                self.queries = []
            def status(self):
                return {"backend": "greenbubbles", "installed": False,
                        "configured": False, "available": False, "verified": False}
            def query(self, *args, **kwargs):
                self.queries.append((args, kwargs))
                raise AssertionError("status must not query the structured reader")
        spy = ReaderSpy()
        service.reader = spy
        status = service.status()
        self.assertEqual(spy.queries, [])
        self.assertTrue(status["readers"]["local_index"]["available"])
        self.assertFalse(status["readers"]["structured_db"]["available"])
        self.assertFalse(status["readers"]["structured_db"]["configured"])
        self.assertFalse(status["capabilities"]["structured_db_read"])
        self.assertTrue(status["capabilities"]["local_index_read"])
        # No TCC/FDA verification is ever claimed by the status contract.
        self.assertFalse(status["capabilities"]["live_read_verified"])
        self.assertEqual(Path(status["db_path"]), root / "index.db")


@POSIX_ONLY
class PeerBoundaryTest(unittest.TestCase):
    """Peer UID, socket file mode and socket-path ancestor checks."""

    def test_server_rejects_wrong_uid_without_reflection_or_state_change(self):
        service = StatusOnlyService()
        rejected = _exchange(service, None, uid=os.getuid() + 1)
        self.assertEqual(rejected, {"ok": False, "error": "provider_ipc_failed"})
        self.assertEqual(service.status_calls, 0)
        self.assertEqual(service.read_calls, [])
        serialized = json.dumps(rejected)
        self.assertNotIn("wrong_client_user", serialized)
        self.assertNotIn("PermissionError", serialized)
        # A rejected peer cannot mutate the read-access state machine.
        ipc = _exchange(service, {"version": 1, "method": "status"})["result"]["ipc"]
        self.assertEqual(ipc["read_access"], "not_checked")

    def test_client_rejects_wrong_service_uid(self):
        root = _temp_dir(self)
        listener = socket.socket(socket.AF_UNIX)
        path = root / "provider.sock"
        listener.bind(str(path))
        path.chmod(0o600)
        listener.listen(1)
        self.addCleanup(listener.close)
        with patch("wechat_provider.local_ipc.peer_uid", return_value=os.getuid() + 1):
            with self.assertRaises(PermissionError) as caught:
                RemoteService(path).status()
        self.assertEqual(str(caught.exception), "wrong_service_user")
        self.assertNotIn(str(root), str(caught.exception))

    def test_client_rejects_unsafe_socket_modes(self):
        root = _temp_dir(self)
        listener = socket.socket(socket.AF_UNIX)
        path = root / "provider.sock"
        listener.bind(str(path))
        listener.listen(1)
        self.addCleanup(listener.close)
        for mode in (0o666, 0o640, 0o604):
            path.chmod(mode)
            with self.assertRaises(PermissionError) as caught:
                RemoteService(path).status()
            self.assertEqual(str(caught.exception), "unsafe_ipc_path")

    def test_client_rejects_symlinked_socket_file(self):
        root = _temp_dir(self)
        listener = socket.socket(socket.AF_UNIX)
        real = root / "real.sock"
        listener.bind(str(real))
        real.chmod(0o600)
        listener.listen(1)
        self.addCleanup(listener.close)
        link = root / "link.sock"
        link.symlink_to(real)
        with self.assertRaises(PermissionError) as caught:
            RemoteService(link).status()
        self.assertEqual(str(caught.exception), "unsafe_ipc_path")

    def test_socket_path_requires_absolute_path(self):
        with self.assertRaises(ValueError) as caught:
            socket_path("relative/provider.sock")
        self.assertEqual(str(caught.exception), "absolute_socket_path_required")

    def test_socket_path_rejects_permissive_parent_directory(self):
        root = _temp_dir(self)
        path = root / "provider.sock"
        self.assertEqual(socket_path(str(path), create=True), path)
        root.chmod(0o755)  # group/world can list and traverse
        with self.assertRaises(PermissionError) as caught:
            socket_path(str(path))
        self.assertEqual(str(caught.exception), "unsafe_ipc_path")

    def test_serve_refuses_permissive_directory_before_creating_anything(self):
        root = _temp_dir(self)
        root.chmod(0o755)
        path = root / "provider.sock"
        with self.assertRaises(PermissionError) as caught:
            serve(str(path), StatusOnlyService())
        self.assertEqual(str(caught.exception), "unsafe_ipc_path")
        self.assertFalse(path.exists())
        self.assertFalse(Path(str(path) + ".lock").exists())

    def test_socket_path_rejects_symlink_ancestor(self):
        root = _temp_dir(self)
        real = root / "real"
        real.mkdir(mode=0o700)
        link = root / "link"
        link.symlink_to(real, target_is_directory=True)
        with self.assertRaises(PermissionError) as caught:
            socket_path(str(link / "provider.sock"), create=True)
        self.assertEqual(str(caught.exception), "symlink_ipc_parent")
        self.assertFalse((real / "provider.sock").exists())


@POSIX_ONLY
class ErrorCodeBoundaryTest(unittest.TestCase):
    """Errors crossing IPC stay in a small stable, non-reflective set."""

    PRIVATE = "/Users/synthetic/Library/Containers/com.tencent.xinWeChat/private.db"

    def _service(self, error):
        private = self.PRIVATE
        class Service:
            def status(self):
                return {"installed": True}
            def save_excerpt(self, chat_title, text):
                raise error
            def get_messages(self, chat_id, limit=50, source="local_index"):
                raise error
            def send_message(self, chat_id, text, idempotency_key=None):
                raise error
        return Service()

    def test_codes_are_stable_and_non_reflective(self):
        cases = [
            ({"version": 1, "method": "save_excerpt", "args": ["x", "y"]},
             ValueError("selected excerpt exceeds local save limit"), "invalid_argument"),
            ({"version": 1, "method": "save_excerpt", "args": ["x", "y"]},
             ValueError(ERROR_GRANT_INVALID), ERROR_GRANT_INVALID),
            ({"version": 1, "method": "save_excerpt", "args": ["x", "y"]},
             ValueError(self.PRIVATE), "invalid_argument"),
            ({"version": 1, "method": "get_messages", "args": ["chat"]},
             AdapterUnavailable("source_access_denied"), "source_access_denied"),
            ({"version": 1, "method": "get_messages", "args": ["chat"]},
             AdapterUnavailable("structured reader not configured: " + self.PRIVATE),
             "reader_unavailable"),
            ({"version": 1, "method": "send_message", "args": ["chat", "text"]},
             RuntimeError("token=PRIVATE " + self.PRIVATE), "provider_ipc_failed"),
            ({"version": 1, "method": "send_message", "args": ["chat", "text"]},
             PermissionError("wrong_client_user " + self.PRIVATE), "provider_ipc_failed"),
            ({"version": 1, "method": "send_message", "args": ["chat", "text"]},
             TypeError("bad argument " + self.PRIVATE), "invalid_argument"),
        ]
        for request, error, expected in cases:
            response = _exchange(self._service(error), request)
            self.assertEqual(response, {"ok": False, "error": expected})
            self.assertIn(response["error"], ALLOWED_IPC_ERRORS)
            serialized = json.dumps(response)
            for leak in ("/Users/synthetic", "com.tencent", "PRIVATE",
                         "wrong_client_user", "selected excerpt", "structured reader"):
                self.assertNotIn(leak, serialized)
            self.assertEqual(set(response), {"ok", "error"})

    def test_allowlist_violations_are_invalid_argument_and_never_called(self):
        class Service:
            called = False
            def status(self):
                return {}
            def __getattr__(self, name):
                # Any method not in METHODS must never be reached.
                Service.called = True
                raise AssertionError(name)
        service = Service()
        for request in (
            {"version": 1, "method": "__dict__"},
            {"version": 1, "method": "shell", "args": ["rm -rf /"]},
            {"version": 2, "method": "status"},
            {"method": "status"},
        ):
            response = _exchange(service, request)
            self.assertEqual(response, {"ok": False, "error": "invalid_argument"})
        self.assertFalse(Service.called)


@POSIX_ONLY
class FrameBoundaryTest(unittest.TestCase):
    """Exactly MAX_FRAME is processed; one byte more is rejected before dispatch."""

    def _frame(self, extra_bytes):
        request = {"version": 1, "method": "search", "args": [""]}
        base = len(json.dumps(request).encode()) + 1  # + newline
        payload_length = MAX_FRAME - base + extra_bytes
        frame = json.dumps({**request, "args": ["x" * payload_length]}).encode() + b"\n"
        return frame, payload_length

    def test_frame_at_exact_limit_is_processed(self):
        class Service:
            def __init__(self):
                self.seen = []
            def search(self, query, chat_id=None, limit=30, source="local_index", cursor=None):
                self.seen.append(len(query))
                return {"matches": [], "source": "local_index"}
        service = Service()
        frame, payload_length = self._frame(0)
        self.assertEqual(len(frame), MAX_FRAME)
        response = _raw_exchange(service, frame)
        self.assertTrue(response["ok"], response)
        self.assertEqual(service.seen, [payload_length])

    def test_frame_one_byte_over_limit_is_rejected_before_dispatch(self):
        class Service:
            def __init__(self):
                self.called = False
            def search(self, query, chat_id=None, limit=30, source="local_index", cursor=None):
                self.called = True
                return {"matches": []}
        service = Service()
        frame, _ = self._frame(1)
        self.assertEqual(len(frame), MAX_FRAME + 1)
        response = _raw_exchange(service, frame)
        self.assertEqual(response, {"ok": False, "error": "invalid_argument"})
        self.assertFalse(service.called)


@POSIX_ONLY
class DaemonFallbackBoundaryTest(unittest.TestCase):
    """Real daemon with WECHAT_PROVIDER_READER=unconfigured (no FDA/TCC path)."""

    @classmethod
    def setUpClass(cls):
        cls.root = Path(tempfile.mkdtemp(prefix="wc-ipc-daemon-")).resolve()
        cls.root.chmod(0o700)
        cls.sock_path = cls.root / "ipc" / "provider.sock"
        cls.db_path = cls.root / "index.db"
        cls.env = {key: value for key, value in os.environ.items()
                   if not key.startswith("WECHAT_PROVIDER_")}
        package_root = Path(wechat_provider.__file__).resolve().parents[1]
        cls.env["PYTHONPATH"] = os.pathsep.join(
            [str(package_root), cls.env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
        cls.env.update({"WECHAT_PROVIDER_DB": str(cls.db_path),
                        "WECHAT_PROVIDER_READER": "unconfigured"})
        cls.process = subprocess.Popen(
            [sys.executable, "-m", "wechat_provider.cli", "daemon",
             "--socket", str(cls.sock_path)],
            env=cls.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(150):
                if cls.sock_path.exists():
                    break
                if cls.process.poll() is not None:
                    raise AssertionError("daemon exited before creating the socket")
                time.sleep(0.02)
            else:
                raise AssertionError("daemon did not create the socket in time")
            # bind() creates the file before listen(); retry until it accepts.
            for _ in range(150):
                try:
                    cls.remote = RemoteService(cls.sock_path)
                    cls.remote.status()
                    break
                except OSError:
                    if cls.process.poll() is not None:
                        raise AssertionError("daemon exited before accepting connections")
                    time.sleep(0.02)
            else:
                raise AssertionError("daemon did not accept connections in time")
        except Exception:
            cls._stop()
            shutil.rmtree(cls.root, ignore_errors=True)
            raise

    @classmethod
    def _stop(cls):
        process = getattr(cls, "process", None)
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

    @classmethod
    def tearDownClass(cls):
        cls._stop()
        shutil.rmtree(cls.root, ignore_errors=True)

    def test_served_socket_and_lock_are_private(self):
        directory = self.sock_path.parent
        directory_info = directory.stat()
        self.assertTrue(stat.S_ISDIR(directory_info.st_mode))
        self.assertEqual(stat.S_IMODE(directory_info.st_mode), 0o700)
        self.assertEqual(directory_info.st_uid, os.getuid())
        socket_info = self.sock_path.stat()
        self.assertTrue(stat.S_ISSOCK(socket_info.st_mode))
        self.assertEqual(stat.S_IMODE(socket_info.st_mode), 0o600)
        self.assertEqual(socket_info.st_uid, os.getuid())
        lock_info = Path(str(self.sock_path) + ".lock").stat()
        self.assertEqual(stat.S_IMODE(lock_info.st_mode), 0o600)
        self.assertEqual(lock_info.st_uid, os.getuid())

    def test_no_fda_fallback_status_reports_local_index_only(self):
        status = self.remote.status()
        self.assertEqual(status["readers"]["local_index"], {"available": True})
        structured = status["readers"]["structured_db"]
        self.assertFalse(structured["available"])
        self.assertFalse(structured["configured"])
        self.assertFalse(structured.get("installed", False))
        self.assertFalse(status["capabilities"]["structured_db_read"])
        self.assertTrue(status["capabilities"]["local_index_read"])
        ipc = status["ipc"]
        self.assertEqual(ipc["read_access"], "not_checked")
        self.assertIs(ipc["tcc_verified"], False)
        self.assertEqual(ipc["permission_owner"], "independent_provider")
        # Only Provider-owned temp paths: no real WeChat data location is touched.
        self.assertEqual(Path(status["db_path"]), self.db_path)
        self.assertTrue(str(status["native_export"]["grant_dir"]).startswith(str(self.root)))
        serialized = json.dumps(status, ensure_ascii=False)
        for real_path in ("com.tencent", "/Library/Containers/", "Application Support/com.tencent"):
            self.assertNotIn(real_path, serialized)

    def test_local_index_read_works_without_fda(self):
        chats = self.remote.list_chats(5, "local_index")
        self.assertEqual(chats["chats"], [])
        self.assertEqual(chats["source"], "local_index")
        self.assertIs(chats["live_read"], False)
        messages = self.remote.get_messages("虚构会话", source="local_index")
        self.assertEqual(messages["messages"], [])
        self.assertEqual(messages["source"], "local_index")
        self.assertIs(messages["live_read"], False)
        # A local-index read is not evidence of structured/WeChat DB access.
        ipc = self.remote.status()["ipc"]
        self.assertEqual(ipc["read_access"], "not_checked")
        self.assertIs(ipc["tcc_verified"], False)

    def test_unconfigured_structured_reader_fails_closed(self):
        with self.assertRaises(RuntimeError) as caught:
            self.remote.get_messages("虚构会话", source="structured_db")
        self.assertEqual(str(caught.exception), "reader_unavailable")
        status = self.remote.status()
        # "Not configured" is not "access denied": no permission was claimed.
        self.assertEqual(status["ipc"]["read_access"], "not_checked")
        self.assertIs(status["ipc"]["tcc_verified"], False)
        self.assertFalse(status["capabilities"]["structured_db_read"])

    @unittest.skipUnless(MCP_AVAILABLE, "MCP client dependency not installed")
    def test_proxy_exposes_tools_and_fails_closed(self):
        proxy = subprocess.Popen(
            [sys.executable, "-m", "wechat_provider.cli", "proxy",
             "--socket", str(self.sock_path)],
            env=self.env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True)
        try:
            def request(frame):
                proxy.stdin.write(json.dumps(frame) + "\n")
                proxy.stdin.flush()
                return _read_json_line(proxy.stdout)
            initialized = request({
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                           "clientInfo": {"name": "fixture", "version": "1"}}})
            self.assertIn("result", initialized)
            proxy.stdin.write(json.dumps(
                {"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
            proxy.stdin.flush()
            local = request({
                "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "wechat_list_chats",
                           "arguments": {"source": "local_index", "limit": 5}}})
            self.assertIs(local["result"]["isError"], False)
            structured = request({
                "jsonrpc": "2.0", "id": 3, "method": "tools/call",
                "params": {"name": "wechat_list_chats",
                           "arguments": {"source": "structured_db", "limit": 5}}})
            self.assertIs(structured["result"]["isError"], True)
            text = structured["result"]["content"][0]["text"]
            self.assertIn("reader_unavailable", text)
            self.assertNotIn(str(self.root), text)
            self.assertNotIn("com.tencent", text)
            # The proxy is a client: the daemon keeps serving after a failed read.
            self.assertIsNone(self.process.poll())
        finally:
            for stream in (proxy.stdin, proxy.stdout):
                try:
                    stream.close()
                except OSError:
                    pass
            proxy.terminate()
            try:
                proxy.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proxy.kill()
                proxy.wait(timeout=5)


def _read_json_line(stream, timeout=15):
    """Read one JSON line without risking an unbounded CI hang."""
    box = {}
    def read():
        try:
            box["line"] = stream.readline()
        except Exception as error:  # pragma: no cover - defensive
            box["error"] = error
    thread = threading.Thread(target=read, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        raise AssertionError("timed out waiting for the MCP proxy response")
    if "error" in box:
        raise box["error"]
    line = box.get("line") or ""
    if not line.strip():
        raise AssertionError("MCP proxy closed stdout without a response")
    return json.loads(line)


if __name__ == "__main__":
    unittest.main()
