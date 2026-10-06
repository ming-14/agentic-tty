"""协议客户端（`example/daemon_test_console/client.py`）：连上、收帧、掉线。

客户端只碰公共层，所以这里用一个**裸监听点**当对端——不必起守护进程，也不必碰 Tk。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from uuid import uuid4

from agentic_tty.example.daemon_test_console.client import Client
from agentic_tty.transport.pipe import PipeTransport, pipe_address
from agentic_tty.transport.stream import parse_address


def _wait_for(predicate: Callable[[], bool], timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_client_reports_a_dropped_connection(tmp_path):
    """对端一走，客户端要如实说"没连上"——上层靠 `connected` 决定要不要重连。"""
    address = pipe_address(f"agentic-tty-test-{uuid4().hex[:8]}", tmp_path / "run")
    listener = PipeTransport().listen(parse_address(address))
    client = Client(address, on_reply=lambda _answer: None)
    try:
        client.connect()
        assert client.connected

        server_side = listener.accept(timeout=2.0)
        assert server_side is not None
        server_side.close()  # 对端断开

        assert _wait_for(lambda: not client.connected)
    finally:
        client.close()
        listener.close()
