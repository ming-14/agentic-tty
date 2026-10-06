"""协议客户端：连守护进程、发一条请求、等它的答复。

MCP 的调用是**同步**的（一次工具调用 = 一问一答），所以这里没有后台读线程——发出去就
在原地收帧，直到那条 `mid` 的答复回来。字节帧与它对应的请求同 `mid`（帧的键就是 `mid`），
所以两类答复用同一个办法认领。
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any

from ...foundation.logs import get_logger
from ...protocol.contracts.daemon_ipc import STREAM_STDIN
from ...protocol.envelope import Envelope, from_json, make_request, to_json
from ...protocol.errors import ProtocolError
from ...protocol.frame import (
    BytesFrame,
    ControlFrame,
    FrameReader,
    encode_bytes,
    encode_control,
)
from ...transport.errors import ConnectionClosed, TransportError
from ...transport.pipe import PipeTransport
from ...transport.stream import Connection, parse_address

_logger = get_logger("example.daemon_test_mcp_server.client")

_POLL = 0.05
"""recv 的等待粒度。"""
_BUFFER = 1 << 16
_CONNECT_TIMEOUT = 2.0
"""单次连接尝试的等待上限。"""
_CALL_TIMEOUT = 20.0
"""一次请求等答复的总预算。"""


class DaemonClient:
    """一条到守护进程的连接；一次调用 = 一问一答。"""

    def __init__(self, address: str) -> None:
        self._address = address
        self._connection: Connection | None = None
        self._reader: FrameReader | None = None

    @property
    def connected(self) -> bool:
        return self._connection is not None

    def try_connect(self, timeout: float = _CONNECT_TIMEOUT) -> bool:
        """**试一次**连接：连上返回 True，连不上返回 False（不抛）。

        调用方拿它做重试策略——起守护进程那一步要一遍遍试到它就绪，不能闷头重试。
        """
        if self._connection is not None:
            return True
        try:
            connection = PipeTransport().connect(parse_address(self._address), timeout=timeout)
        except TransportError:
            return False
        self._connection = connection
        self._reader = FrameReader(lambda: connection.recv(_BUFFER, timeout=_POLL))
        return True

    def call(self, command: str, op: Mapping[str, Any] | None = None) -> Envelope | BytesFrame:
        """发一条请求，返回它的答复（控制帧或字节帧）。"""
        envelope = make_request(command, op=op or {})
        self._send(encode_control(to_json(envelope)))
        return self._await(envelope.mid)

    def write(self, uid: str, data: bytes) -> None:
        """往某个会话写字节——走字节帧，键是 uid。"""
        self._send(encode_bytes(STREAM_STDIN, uid, data))

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None
            self._reader = None

    # ════════════════════════════════════════════════════════════
    # 内部
    # ════════════════════════════════════════════════════════════

    def _send(self, data: bytes) -> None:
        connection = self._connection
        if connection is None:
            raise TransportError(f"还没连上守护进程: {self._address}")
        try:
            connection.send(data)
        except ConnectionClosed as exc:
            self.close()
            raise TransportError(f"连接已断: {exc}") from exc

    def _await(self, mid: str) -> Envelope | BytesFrame:
        """原地收帧，直到那条 `mid` 的答复回来。"""
        reader = self._reader
        if reader is None:
            raise TransportError("没有连接")
        deadline = time.monotonic() + _CALL_TIMEOUT
        while True:
            try:
                frames = reader.read()
            except (ConnectionClosed, ProtocolError) as exc:
                self.close()
                raise TransportError(f"连接结束: {exc}") from exc
            for frame in frames:
                answer = self._claim(frame, mid)
                if answer is not None:
                    return answer
            if time.monotonic() >= deadline:
                self.close()
                raise TransportError(f"等答复超时: {mid}")

    @staticmethod
    def _claim(frame: ControlFrame | BytesFrame, mid: str) -> Envelope | BytesFrame | None:
        """认领这一帧：不是这条请求的答复就返回 None。"""
        if isinstance(frame, BytesFrame):
            return frame if frame.key == mid else None
        try:
            envelope = from_json(frame.data)
        except ProtocolError as exc:
            _logger.warning("信封解不开: %s", exc)
            return None
        return envelope if envelope.mid == mid else None
