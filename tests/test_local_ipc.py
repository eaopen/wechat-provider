import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from wechat_provider.local_ipc import handle, send, receive, RemoteService, socket_path, MAX_FRAME
from wechat_provider.models import AdapterUnavailable

# The MCP proxy subprocess needs the optional `mcp` package; the minimal CI
# matrix installs no dependencies, so skip instead of failing for that reason.
MCP_AVAILABLE = importlib.util.find_spec("mcp") is not None


@unittest.skipUnless(os.name == 'posix', 'Unix sockets require POSIX')
class IPCTest(unittest.TestCase):
    def exchange(self, request, uid=None, svc=None):
        if svc is None:
            class Service:
                called = False
                def status(self):
                    self.called = True
                    return {'installed': True}
            svc = Service()
        a, b = socket.socketpair()
        def worker():
            with a:
                if uid is None:
                    handle(a, svc)
                else:
                    with patch('wechat_provider.local_ipc.peer_uid', return_value=uid):
                        handle(a, svc)
        t = threading.Thread(target=worker); t.start()
        with b:
            try:
                send(b, request)
            except BrokenPipeError:
                pass  # rejected peer may be disconnected before writing
            response = receive(b)
        t.join(2)
        return response, getattr(svc, "called", False)

    def test_peer_identity_and_allowlist(self):
        good = {'version': 1, 'method': 'status'}
        result, called = self.exchange(good)
        self.assertTrue(result['ok']); self.assertTrue(called)
        self.assertFalse(result['result']['ipc']['tcc_verified'])
        for req, uid in [(good, os.getuid()+1), ({'version': 1, 'method': '__dict__'}, None),
                         ({**good, 'kwargs': {'command': 'do not run'}}, None)]:
            result, called = self.exchange(req, uid)
            self.assertFalse(result['ok']); self.assertFalse(called)

    def test_frame_limit_is_exact(self):
        n = MAX_FRAME - len(b'{"a":""}\n')
        exact = b'{"a":"' + b'x' * n + b'"}\n'
        big = b'{"a":"' + b'x' * (n + 1) + b'"}\n'
        self.assertEqual(len(exact), MAX_FRAME)
        self.assertEqual(len(big), MAX_FRAME + 1)
        for frame, ok in ((exact, True), (big, False)):
            a, b = socket.socketpair()
            def writer(sock=b, data=frame):
                try:
                    sock.sendall(data)
                finally:
                    sock.close()
            t = threading.Thread(target=writer); t.start()
            try:
                with a:
                    if ok:
                        self.assertEqual(receive(a)['a'], 'x' * n)
                    else:
                        with self.assertRaises(ValueError):
                            receive(a)
            finally:
                t.join(2)

    def test_error_codes_are_stable_and_non_reflective(self):
        class Service:
            def status(self):
                return {}
            def save_excerpt(self, chat_title, text):
                raise ValueError('selected excerpt exceeds local save limit')
            def get_messages(self, chat_id, limit=10, source='local_index'):
                raise AdapterUnavailable('structured reader not configured /secret/path')
            def send_message(self, chat_id, text, idempotency_key=None):
                raise RuntimeError('token=PRIVATE')
        svc = Service()
        cases = [
            ({'version': 1, 'method': 'save_excerpt', 'args': ['x', 'y']}, 'invalid_argument'),
            ({'version': 1, 'method': 'get_messages', 'args': ['x']}, 'reader_unavailable'),
            ({'version': 1, 'method': 'send_message', 'args': ['x', 'y']}, 'provider_ipc_failed'),
        ]
        for request, expected in cases:
            result, _ = self.exchange(request, svc=svc)
            self.assertFalse(result['ok'])
            self.assertEqual(result['error'], expected)
        self.assertNotIn('PRIVATE', json.dumps(cases))

    def test_unsafe_directory_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root).resolve(); root.chmod(0o755)
            with self.assertRaises(PermissionError):
                socket_path(str(root/'provider.sock'))

    @unittest.skipUnless(MCP_AVAILABLE, "MCP client dependency not installed")
    def test_process_read_and_mcp_proxy(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root).resolve(); root.chmod(0o700)
            path = root/'provider.sock'
            env = {**os.environ, 'WECHAT_PROVIDER_DB': str(root/'index.db'),
                   'WECHAT_PROVIDER_READER': 'unconfigured'}
            process = subprocess.Popen([sys.executable, '-m', 'wechat_provider.cli', 'daemon',
                                        '--socket', str(path)], env=env,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                for _ in range(100):
                    if path.exists(): break
                    if process.poll() is not None: self.fail('daemon exited')
                    time.sleep(.02)
                remote = RemoteService(path)
                self.assertEqual(remote.list_chats(5, 'local_index')['chats'], [])
                self.assertTrue(remote.status()['ipc']['same_user'])
                # Synthetic-only explicit retention, with the same UID/IPC contract.
                saved = remote.save_excerpt('虚构聊天', '我：测试片段')
                self.assertEqual(saved['imported'], 1)
                self.assertEqual(remote.save_excerpt('虚构聊天', '我：测试片段')['duplicates'], 1)
                self.assertEqual(len(remote.list_chats(5, 'local_index')['chats']), 1)
                self.assertEqual(remote.forget_saved_excerpts('虚构聊天')['removed'], 1)
                self.assertEqual(remote.list_chats(5, 'local_index')['chats'], [])
                # Actual MCP initialization, discovery and tool response through proxy.
                frames = [
                    {'jsonrpc':'2.0','id':1,'method':'initialize','params':{
                        'protocolVersion':'2024-11-05','capabilities':{},
                        'clientInfo':{'name':'fixture','version':'1'}}},
                    {'jsonrpc':'2.0','method':'notifications/initialized'},
                    {'jsonrpc':'2.0','id':2,'method':'tools/call','params':{
                        'name':'wechat_list_chats','arguments':{'source':'local_index','limit':5}}}]
                result = subprocess.run([sys.executable,'-m','wechat_provider.cli','proxy','--socket',str(path)],
                    input=''.join(json.dumps(f)+'\n' for f in frames), capture_output=True,
                    text=True, timeout=15, env=env)
                responses = [json.loads(line) for line in result.stdout.splitlines()]
                reply = next(r for r in responses if r.get('id') == 2)
                self.assertFalse(reply['result'].get('isError', False))
            finally:
                process.terminate(); process.wait(timeout=5)
            with self.assertRaises((ConnectionError, OSError)):
                RemoteService(path).status()
