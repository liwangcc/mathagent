"""Run selected expansion tests with isolated storage and no external network.

Usage: venv_local/Scripts/python.exe scripts/run_math_stage_tests.py [pytest args]
Only temporary test data is written. Existing dotenv/DB/uploads are not loaded.
"""
from __future__ import annotations

import ipaddress
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import weakref


def _is_local_host(host) -> bool:
    if host is None:
        return True
    if isinstance(host, bytes):
        host = host.decode("ascii", "ignore")
    host = str(host).strip().strip("[]")
    if host in {"", "localhost"}:
        return True
    try:
        address = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return False
    return address.is_loopback or address.is_unspecified


def restrict_network_to_test_servers():
    """Allow only live loopback listeners bound to port 0 by this process.

    Existing local services, remote endpoints and fixed ports stay inaccessible.
    This permits synthetic real-HTTP worker tests without contacting a real API.
    Remote host names are not resolved either: asyncio's Windows proactor
    connects without ``socket.connect``, so resolution is the choke point that
    also stops async HTTP clients.
    """
    original_bind = socket.socket.bind
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_socketpair = socket.socketpair
    original_getaddrinfo = socket.getaddrinfo
    listeners = weakref.WeakValueDictionary()
    handshake = threading.local()

    def test_bind(sock, address):
        result = original_bind(sock, address)
        if (isinstance(address, tuple) and len(address) >= 2
                and address[0] in {"127.0.0.1", "::1"} and address[1] == 0):
            bound = sock.getsockname()
            listeners[(bound[0], bound[1])] = sock
        return result

    def allowed(address):
        if getattr(handshake, "active", False):
            return True
        if not isinstance(address, tuple) or len(address) < 2:
            return False
        server = listeners.get((address[0], address[1]))
        return server is not None and server.fileno() >= 0

    def connect(sock, address):
        if not allowed(address):
            raise RuntimeError("External or pre-existing service access is disabled in isolated tests")
        return original_connect(sock, address)

    def connect_ex(sock, address):
        if not allowed(address):
            raise RuntimeError("External or pre-existing service access is disabled in isolated tests")
        return original_connect_ex(sock, address)

    def local_socketpair(*args, **kwargs):
        # Windows socketpair uses a private loopback handshake.
        handshake.active = True
        try:
            return original_socketpair(*args, **kwargs)
        finally:
            handshake.active = False

    def local_getaddrinfo(host, *args, **kwargs):
        if not _is_local_host(host):
            raise socket.gaierror(
                socket.EAI_NONAME,
                "External name resolution is disabled in isolated tests",
            )
        return original_getaddrinfo(host, *args, **kwargs)

    socket.socket.bind = test_bind
    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex
    socket.socketpair = local_socketpair
    socket.getaddrinfo = local_getaddrinfo

    def restore():
        socket.socket.bind = original_bind
        socket.socket.connect = original_connect
        socket.socket.connect_ex = original_connect_ex
        socket.socketpair = original_socketpair
        socket.getaddrinfo = original_getaddrinfo
    return restore


def main() -> int:
    repository = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(repository))
    os.environ["PYTHON_DOTENV_DISABLED"] = "1"
    os.environ["JWT_SECRET"] = "isolated-school-stage-test-secret-not-for-production"
    os.environ["REDIS_URL"] = ""
    os.environ["KNOWLEDGE_CLASSIFIER_WORKER_ENABLED"] = "false"
    # Do not inherit upstream credentials from the calling terminal.
    for key in tuple(os.environ):
        if key.endswith(("_API_KEY", "_API_KEYS", "_BASE_URL", "_ACCESS_KEY_SECRET")):
            os.environ.pop(key, None)

    restrict_network_to_test_servers()
    output_dir = repository / "outputs"
    output_dir.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="mathagent-school-stage-", dir=output_dir, ignore_cleanup_errors=True,
    ) as temporary:
        root = Path(temporary)
        os.environ["DATABASE_URL"] = f"sqlite:///{(root / 'test.db').as_posix()}"
        os.environ["MATHAGENT_DATA_DIR"] = str(root)
        import config

        for name in ("UPLOAD_DIR", "OUTPUT_DIR", "DATA_DIR", "FAILED_DIR", "ARCHIVE_DIR"):
            folder = root / name.lower()
            folder.mkdir(exist_ok=True)
            setattr(config, name, str(folder))
        # Importing app.py points tempfile at the monitored upload spool. Unit
        # tests mock the normal storage-admission middleware, so prepare only
        # this synthetic directory before test collection imports the app.
        (Path(config.UPLOAD_DIR) / ".multipart-tmp").mkdir(exist_ok=True)
        import pytest

        return pytest.main(["-p", "no:cacheprovider", "--basetemp", str(root / "pytest")] + (sys.argv[1:] or [
            "-q", "tests/test_teaching_context.py", "tests/test_teaching_context_sessions.py",
            "tests/test_math_curriculum.py",
        ]))


if __name__ == "__main__":
    raise SystemExit(main())
