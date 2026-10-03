"""本机管道传输：一个名字、多条连接、双向字节流。

这条传输的关键性质只有一条：**同一个名字上能同时接多条互不干扰的连接**——"多个消费者
连同一个守护进程"就靠它。所以下面每条用例都围着它转，其中"跨进程"那条必须真起子进程，
同进程内的两条线程测不出命名管道真正的脾气。
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from agentic_tty.transport import registry as transports
from agentic_tty.transport.errors import ConnectionClosed, TransportError
from agentic_tty.transport.pipe import pipe_address, pipe_path
from agentic_tty.transport.stream import parse_address

_DEADLINE = 10.0
_SRC = str(Path(__file__).resolve().parents[2] / "src")
"""子进程要能 import 本包：给它源码目录，别指望测试是从哪儿启动的。"""

_CHILD = """
import sys
sys.path.insert(0, sys.argv[2])
from agentic_tty.transport import registry as transports
conn = transports.connect(sys.argv[1], timeout=10)
conn.send(b"from-child")
conn.recv(1024, timeout=5)
conn.close()
"""


def _uri(name: str) -> str:
    return f"pipe://{name}"


def _unique(prefix: str) -> str:
    return f"{prefix}-{int(time.time() * 1000) % 1_000_000}-{threading.get_ident() % 1000}"


@pytest.fixture
def name():
    """每个用例一个独立名字：名字就是全局命名空间，撞上会互相干扰。"""
    return _unique("test")


def test_scheme_is_registered():
    assert "pipe" in transports.schemes()


def test_bytes_travel_both_ways(name):
    listener = transports.listen(_uri(name))
    try:
        got: list[bytes] = []

        def client():
            conn = transports.connect(_uri(name), timeout=_DEADLINE)
            conn.send(b"ping")
            got.append(conn.recv(1024, timeout=_DEADLINE))
            conn.close()

        thread = threading.Thread(target=client, daemon=True)
        thread.start()
        conn = listener.accept(timeout=_DEADLINE)
        assert conn is not None
        try:
            assert conn.recv(1024, timeout=_DEADLINE) == b"ping"
            conn.send(b"pong")
        finally:
            conn.close()
        thread.join(_DEADLINE)
        assert got == [b"pong"]
    finally:
        listener.close()


def test_one_name_carries_several_connections_that_do_not_mix(name):
    """**这条是本模块的存在理由**：一个名字、多条连接、各自的字节不串。"""
    listener = transports.listen(_uri(name))
    try:
        got: dict[int, int] = {}

        def client(mark: int):
            conn = transports.connect(_uri(name), timeout=_DEADLINE)
            conn.send(bytes([mark]) * 4096)
            got[mark] = len(conn.recv(65536, timeout=_DEADLINE))
            conn.close()

        threads = [
            threading.Thread(target=client, args=(mark,), daemon=True) for mark in (1, 2)
        ]
        for thread in threads:
            thread.start()
        # 两个客户端**同时**在连：监听点必须交出一条后立刻备好下一条实例
        connections = [listener.accept(timeout=_DEADLINE) for _ in range(2)]
        try:
            assert all(conn is not None for conn in connections)
            for index, conn in enumerate(connections):
                first = conn.recv(65536, timeout=_DEADLINE)
                assert first == bytes([index + 1]) * 4096  # 各收各的，没串
                conn.send(b"x" * 8192)
        finally:
            for conn in connections:
                assert conn is not None
                conn.close()
        for thread in threads:
            thread.join(_DEADLINE)
        assert got == {1: 8192, 2: 8192}
    finally:
        listener.close()


def test_accept_returns_none_on_timeout(name):
    listener = transports.listen(_uri(name))
    try:
        assert listener.accept(timeout=0.1) is None
    finally:
        listener.close()


def test_peer_closing_raises_connection_closed(name):
    listener = transports.listen(_uri(name))
    try:
        threading.Thread(
            target=lambda: transports.connect(_uri(name), timeout=_DEADLINE).close(),
            daemon=True,
        ).start()
        conn = listener.accept(timeout=_DEADLINE)
        assert conn is not None
        try:
            with pytest.raises(ConnectionClosed):
                for _ in range(40):
                    time.sleep(0.05)
                    conn.recv(1024, timeout=0.05)
        finally:
            conn.close()
    finally:
        listener.close()


def test_recv_returns_empty_while_quiet(name):
    listener = transports.listen(_uri(name))
    try:
        threading.Thread(
            target=lambda: transports.connect(_uri(name), timeout=_DEADLINE),
            daemon=True,
        ).start()
        conn = listener.accept(timeout=_DEADLINE)
        assert conn is not None
        try:
            assert conn.recv(1024, timeout=0.1) == b""  # 空 ≠ 关闭
        finally:
            conn.close()
    finally:
        listener.close()


def test_real_process_can_connect(name, tmp_path: Path):
    """命名管道必须**真跨进程**才算过：同进程内的两条线程测不出多实例的脾气。"""
    child_script = tmp_path / "child.py"
    child_script.write_text(_CHILD, encoding="utf-8")
    listener = transports.listen(_uri(name))
    child = None
    try:
        child = subprocess.Popen([sys.executable, str(child_script), _uri(name), _SRC])
        conn = listener.accept(timeout=_DEADLINE)
        assert conn is not None
        try:
            assert conn.recv(1024, timeout=_DEADLINE) == b"from-child"
            conn.send(b"to-child")
        finally:
            conn.close()
        assert child.wait(_DEADLINE) == 0
    finally:
        if child is not None and child.poll() is None:
            child.kill()
        listener.close()


def test_large_payload_arrives_intact(name):
    """一次发一大块：收侧要能分多次收全，字节一字不差。"""
    listener = transports.listen(_uri(name))
    try:
        payload = bytes(range(256)) * 4096  # 1 MiB

        def client():
            conn = transports.connect(_uri(name), timeout=_DEADLINE)
            conn.send(payload)
            conn.close()

        threading.Thread(target=client, daemon=True).start()
        conn = listener.accept(timeout=_DEADLINE)
        assert conn is not None
        try:
            received = bytearray()
            deadline = time.monotonic() + _DEADLINE
            while len(received) < len(payload) and time.monotonic() < deadline:
                try:
                    received.extend(conn.recv(65536, timeout=0.2))
                except ConnectionClosed:
                    break
            assert bytes(received) == payload
        finally:
            conn.close()
    finally:
        listener.close()


def test_connect_to_missing_name_is_refused():
    with pytest.raises(TransportError):
        transports.connect(_uri(_unique("nobody")), timeout=0.3)


def test_listen_needs_a_name():
    with pytest.raises(TransportError):
        transports.listen("pipe://")


def test_endpoint_follows_the_runtime_dir_in_the_address(tmp_path: Path):
    """端点位置由**地址**给出——锁 / 端点因此落在同一个目录里。"""
    name = _unique("probe")
    here = parse_address(pipe_address(name, tmp_path))
    there = parse_address(pipe_address(name, tmp_path / "other"))

    if sys.platform == "win32":
        # 命名管道名是全局的、没有目录——目录被并进名字：同目录同名，异目录异名。
        assert pipe_path(here) == pipe_path(parse_address(pipe_address(name, tmp_path)))
        assert pipe_path(here) != pipe_path(there)
    else:
        assert pipe_path(here) == str(tmp_path / f"agentic-tty-{name}.sock")
        assert pipe_path(there) == str(tmp_path / "other" / f"agentic-tty-{name}.sock")


def test_address_without_a_dir_uses_the_platform_default():
    """不给目录就用平台默认——`pipe://<名字>` 的旧写法照旧。"""
    name = _unique("probe")
    assert pipe_path(parse_address(pipe_address(name))) == pipe_path(parse_address(_uri(name)))
