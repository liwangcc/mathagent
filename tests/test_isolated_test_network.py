import socket

import pytest

from scripts.run_math_stage_tests import restrict_network_to_test_servers


def test_guard_only_allows_new_synthetic_loopback_servers():
    existing = socket.socket()
    existing.bind(("127.0.0.1", 0))
    existing.listen()
    restore = restrict_network_to_test_servers()
    try:
        for address in (existing.getsockname(), ("127.0.0.1", 8080), ("192.0.2.1", 443)):
            with socket.socket() as client:
                with pytest.raises(RuntimeError, match="disabled"):
                    client.connect(address)
                with pytest.raises(RuntimeError, match="disabled"):
                    client.connect_ex(address)
        with socket.socket() as server:
            server.bind(("127.0.0.1", 0))
            server.listen()
            address = server.getsockname()
            with socket.socket() as client:
                client.connect(address)
                peer, _ = server.accept()
                peer.close()
            with socket.socket() as client:
                assert client.connect_ex(address) == 0
                peer, _ = server.accept()
                peer.close()
        with socket.socket() as client:
            with pytest.raises(RuntimeError, match="disabled"):
                client.connect(address)
        left, right = socket.socketpair()
        left.close()
        right.close()
    finally:
        restore()
        existing.close()


def test_guard_refuses_remote_name_resolution():
    restore = restrict_network_to_test_servers()
    try:
        for host in ("dashscope.aliyuncs.com", b"api.example.com", "192.0.2.1"):
            with pytest.raises(socket.gaierror, match="disabled"):
                socket.getaddrinfo(host, 443)
        assert socket.getaddrinfo("127.0.0.1", 0)
        assert socket.getaddrinfo("localhost", 0)
    finally:
        restore()
