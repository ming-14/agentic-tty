from __future__ import annotations

import threading
import time

import pytest

from agentic_tty.transport import registry
from agentic_tty.transport.errors import ConnectionClosed, TransportError
from agentic_tty.transport.stream import Connection, Listener

_DEADLINE = 5.0


@pytest.fixture
def listener() -> Listener:
    lst = registry.listen("tcp://127.0.0.1:0")
    try:
        yield lst
    finally:
        lst.close()


def _read_exact(conn: Connection, size: int) -> bytes:
    buf = bytearray()
    deadline = time.monotonic() + _DEADLINE
    while len(buf) < size:
        assert time.monotonic() < deadline, f"只读到 {len(buf)}/{size} 字节"
        buf.extend(conn.recv(max_bytes=size - len(buf), timeout=0.1))
    return bytes(buf)


def _accept(listener: Listener) -> Connection:
    conn = listener.accept(timeout=_DEADLINE)
    assert conn is not None
    return conn


def test_listen_reports_the_kernel_assigned_port(listener: Listener):
    host, port = listener.address.host_port
    assert host == "127.0.0.1"
    assert port > 0
    assert str(listener.address).startswith("tcp://127.0.0.1:")


def test_roundtrip_both_ways(listener: Listener):
    client = registry.connect(listener.address.raw, timeout=_DEADLINE)
    server = _accept(listener)
    try:
        client.send(b"ping")
        assert _read_exact(server, 4) == b"ping"
        server.send(b"pong")
        assert _read_exact(client, 4) == b"pong"
    finally:
        client.close()
        server.close()


def test_large_payload_survives(listener: Listener):
    client = registry.connect(listener.address.raw, timeout=_DEADLINE)
    server = _accept(listener)
    blob = bytes(range(256)) * 4096  # 1 MiB
    try:
        client.send(blob)
        assert _read_exact(server, len(blob)) == blob
    finally:
        client.close()
        server.close()


def test_recv_timeout_returns_empty(listener: Listener):
    client = registry.connect(listener.address.raw, timeout=_DEADLINE)
    server = _accept(listener)
    try:
        assert client.recv(timeout=0.05) == b""
    finally:
        client.close()
        server.close()


def test_recv_with_zero_timeout_polls_instead_of_claiming_eof(listener: Listener):
    """`timeout=0` 走的是非阻塞路径（抛 `BlockingIOError`），不能把它当成对端已关闭。"""
    client = registry.connect(listener.address.raw, timeout=_DEADLINE)
    server = _accept(listener)
    try:
        assert client.recv(timeout=0) == b""
        client.send(b"still alive")
        assert _read_exact(server, 11) == b"still alive"
    finally:
        client.close()
        server.close()


def test_accept_timeout_returns_none(listener: Listener):
    assert listener.accept(timeout=0.05) is None


def test_peer_close_is_reported_as_closed(listener: Listener):
    client = registry.connect(listener.address.raw, timeout=_DEADLINE)
    server = _accept(listener)
    try:
        server.close()
        deadline = time.monotonic() + _DEADLINE
        while True:
            assert time.monotonic() < deadline
            try:
                client.recv(timeout=0.1)
            except ConnectionClosed:
                break
    finally:
        client.close()


def test_send_after_close_is_reported(listener: Listener):
    client = registry.connect(listener.address.raw, timeout=_DEADLINE)
    server = _accept(listener)
    try:
        client.close()
        with pytest.raises(ConnectionClosed):
            client.send(b"x")
    finally:
        server.close()


def test_close_is_idempotent(listener: Listener):
    client = registry.connect(listener.address.raw, timeout=_DEADLINE)
    client.close()
    client.close()


def test_connecting_to_a_closed_port_fails():
    probe = registry.listen("tcp://127.0.0.1:0")
    uri = probe.address.raw
    probe.close()
    with pytest.raises(TransportError, match="连接"):
        registry.connect(uri, timeout=2.0)


def test_connect_requires_a_port():
    with pytest.raises(TransportError, match="端口"):
        registry.connect("tcp://127.0.0.1", timeout=2.0)


def test_send_and_recv_may_run_on_two_threads(listener: Listener):
    """写会在对端不读时阻塞，所以发送必须能独立于接收跑在另一个线程上。"""
    client = registry.connect(listener.address.raw, timeout=_DEADLINE)
    server = _accept(listener)
    chunks = [bytes((i,)) * 512 for i in range(8)]
    writer = threading.Thread(target=lambda: [client.send(c) for c in chunks])
    try:
        writer.start()
        received = _read_exact(server, sum(len(c) for c in chunks))
        writer.join(_DEADLINE)
        assert received == b"".join(chunks)
    finally:
        client.close()
        server.close()
