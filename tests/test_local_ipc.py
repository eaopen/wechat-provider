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
from wechat_provider.local_ipc import handle, send, receive, RemoteService, socket_path


@unittest.skipUnless(os.name == 'posix', 'Unix sockets require POSIX')
class IPCTest(unittest.TestCase):
    def exchange(self, request, uid=None):
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
        return response, svc.called

    def test_peer_identity_and_allowlist(self):
        good = {'version': 1, 'method': 'status'}
        result, called = self.exchange(good)
        self.assertTrue(result['ok']); self.assertTrue(called)
        self.assertFalse(result['result']['ipc']['tcc_verified'])
        for req, uid in [(good, os.getuid()+1), ({'version': 1, 'method': '__dict__'}, None),
                         ({**good, 'kwargs': {'command': 'do not run'}}, None)]:
            result, called = self.exchange(req, uid)
            self.assertFalse(result['ok']); self.assertFalse(called)

    def test_unsafe_directory_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root).resolve(); root.chmod(0o755)
            with self.assertRaises(PermissionError):
                socket_path(str(root/'provider.sock'))

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
