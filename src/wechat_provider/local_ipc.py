"""Same-user, bounded local RPC. No command execution or automatic service startup."""
from __future__ import annotations
import ctypes
import inspect
import json
import os
from pathlib import Path
import socket
import stat
import struct

MAX_FRAME = 1024 * 1024
METHODS = frozenset(('status', 'list_chats', 'get_messages', 'search',
                     'send_message', 'get_send_status'))


def peer_uid(connection):
    if hasattr(socket, 'SO_PEERCRED'):
        return struct.unpack('3i', connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[1]
    uid, gid = ctypes.c_uint(), ctypes.c_uint()
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.getpeereid(connection.fileno(), ctypes.byref(uid), ctypes.byref(gid)):
        raise PermissionError('peer_identity_unavailable')
    return uid.value


def require_owner(path, directory=False):
    info = path.lstat()
    if (info.st_uid != os.getuid() or stat.S_ISLNK(info.st_mode)
            or info.st_mode & 0o077
            or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISSOCK(info.st_mode))):
        raise PermissionError('unsafe_ipc_path')


def socket_path(value, create=False):
    path = Path(value)
    if not path.is_absolute():
        raise ValueError('absolute_socket_path_required')
    # Reject symlink ancestors, including user-selected locations.
    for parent in path.parents:
        if parent.is_symlink():
            raise PermissionError('symlink_ipc_parent')
    if create:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    require_owner(path.parent, directory=True)
    return path


def receive(connection):
    data = bytearray()
    while len(data) <= MAX_FRAME:
        chunk = connection.recv(min(65536, MAX_FRAME + 1 - len(data)))
        if not chunk:
            raise ValueError('incomplete_frame')
        data.extend(chunk)
        if b'\n' in chunk:
            if not data.endswith(b'\n') or data.count(b'\n') != 1:
                raise ValueError('invalid_frame')
            return json.loads(data)
    raise ValueError('frame_too_large')


def send(connection, value):
    encoded = json.dumps(value, ensure_ascii=False).encode() + b'\n'
    if len(encoded) > MAX_FRAME:
        raise ValueError('frame_too_large')
    connection.sendall(encoded)


class RemoteService:
    def __init__(self, path):
        self.path = str(path)

    def __getattr__(self, method):
        if method not in METHODS:
            raise AttributeError(method)
        def call(*args, **kwargs):
            path = socket_path(self.path)
            require_owner(path)
            with socket.socket(socket.AF_UNIX) as connection:
                connection.settimeout(60)
                connection.connect(str(path))
                if peer_uid(connection) != os.getuid():
                    raise PermissionError('wrong_service_user')
                send(connection, {'version': 1, 'method': method, 'args': args, 'kwargs': kwargs})
                result = receive(connection)
            if result.get('ok') is not True:
                raise RuntimeError(result.get('error', 'provider_ipc_failed'))
            return result['result']
        return call


def handle(connection, service):
    connection.settimeout(60)
    try:
        if peer_uid(connection) != os.getuid():
            raise PermissionError('wrong_client_user')
        request = receive(connection)
        if request.get('version') != 1 or request.get('method') not in METHODS:
            raise ValueError('unsupported_request')
        method = getattr(service, request['method'])
        args, kwargs = request.get('args', []), request.get('kwargs', {})
        inspect.signature(method).bind(*args, **kwargs)
        result = method(*args, **kwargs)
        if request.get('kwargs', {}).get('source') == 'structured_db' or (
                isinstance(result, dict) and result.get('source') == 'structured_db'):
            service._ipc_read_state = 'verified'
        if request['method'] == 'status':
            result['ipc'] = {'transport': 'unix', 'same_user': True,
                             'permission_owner': 'independent_provider',
                             'tcc_verified': False,
                             'read_access': getattr(service, '_ipc_read_state', 'not_checked')}
        send(connection, {'ok': True, 'result': result})
    except Exception as error:
        # Never reflect private paths, SQL, message text or backend exception details.
        if 'source_access_denied' in str(error):
            service._ipc_read_state = 'access_denied'
        code = 'source_access_denied' if 'source_access_denied' in str(error) else 'provider_ipc_failed'
        try:
            send(connection, {'ok': False, 'error': code})
        except (OSError, ValueError):
            pass


def serve(path, service):
    import fcntl
    path = socket_path(path, create=True)
    lock = os.open(str(path)+'.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        if os.fstat(lock).st_uid != os.getuid():
            raise PermissionError('unsafe_lock')
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if path.exists():
            require_owner(path)
            path.unlink()  # only after acquiring the exclusive service lock
        with socket.socket(socket.AF_UNIX) as listener:
            listener.bind(str(path)); path.chmod(0o600)
            listener.listen(8)
            try:
                while True:
                    connection, _ = listener.accept()
                    with connection:
                        handle(connection, service)
            finally:
                path.unlink(missing_ok=True)
    finally:
        os.close(lock)
