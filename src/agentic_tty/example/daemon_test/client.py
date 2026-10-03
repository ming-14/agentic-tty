"""协议客户端：连守护进程、发请求、收答复。

**只碰 `protocol` / `transport`**——不认识 core，也不认识 daemon。读在**后台线程**里做
（Tk 主线程要跑 `mainloop`、不能阻塞），凑齐的答复经 `on_reply` 交出去，界面在 tick 里
取。请求与答复靠 `mid` 关联；答复可能是控制帧（文本）也可能是字节帧（位图 / 字节流）。
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from ...foundation.logs import get_logger
from ...protocol.contracts.daemon_ipc import STREAM_STDOUT
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

_logger = get_logger("example.daemon_test.client")

_POLL = 0.05
"""recv 的等待粒度；也是读线程"该收工了吗"的响应粒度。"""
_BUFFER = 1 << 16


@dataclass(frozen=True, slots=True)
class Answer:
    """一条答复：`mid` 是它对应的请求，`envelope` 与 `chunk` 二选一。"""

    mid: str
    envelope: Envelope | None = None
    chunk: BytesFrame | None = None


class Client:
    """一条到守护进程的连接。"""

    def __init__(self, address: str, *, on_reply: Callable[[Answer], None]) -> None:
        self._address = address
        self._on_reply = on_reply
        self._connection: Connection | None = None
        self._reader: threading.Thread | None = None
        self._closing = threading.Event()

    @property
    def connected(self) -> bool:
        return self._connection is not None

    def connect(self, timeout: float = 5.0) -> None:
        """连上去并起读线程。"""
        self._connection = PipeTransport().connect(parse_address(self._address), timeout=timeout)
        self._reader = threading.Thread(target=self._read_loop, name="daemon-client", daemon=True)
        self._reader.start()

    def request(self, command: str, op: Mapping[str, Any] | None = None) -> str:
        """发一条控制请求，返回它的 `mid`——答复按这个 `mid` 回来。"""
        envelope = make_request(command, op=op or {})
        self._require().send(encode_control(to_json(envelope)))
        return envelope.mid

    def write(self, uid: str, data: bytes) -> None:
        """往某个会话写字节。**走字节帧**——字节不进 JSON，省掉 base64 的三分之一膨胀。"""
        self._require().send(encode_bytes(STREAM_STDOUT, uid, data))

    def close(self) -> None:
        self._closing.set()
        if self._connection is not None:
            self._connection.close()
            self._connection = None
        if self._reader is not None:
            self._reader.join(_POLL * 4)
            self._reader = None

    # ════════════════════════════════════════════════════════════
    # 内部
    # ════════════════════════════════════════════════════════════

    def _require(self) -> Connection:
        if self._connection is None:
            raise TransportError("还没连上守护进程")
        return self._connection

    def _read_loop(self) -> None:
        """后台读：凑齐一帧交一帧。**只在这里读连接。**"""
        connection = self._connection
        if connection is None:
            return
        reader = FrameReader(lambda: connection.recv(_BUFFER, timeout=_POLL))
        while not self._closing.is_set():
            try:
                frames = reader.read()
            except (ConnectionClosed, ProtocolError) as exc:
                _logger.info("连接结束: %s", exc)
                return
            for frame in frames:
                self._deliver(frame)

    def _deliver(self, frame: ControlFrame | BytesFrame) -> None:
        if isinstance(frame, BytesFrame):
            self._on_reply(Answer(mid=frame.key, chunk=frame))
            return
        try:
            envelope = from_json(frame.data)
        except ProtocolError as exc:
            _logger.warning("信封解不开: %s", exc)
            return
        self._on_reply(Answer(mid=envelope.mid, envelope=envelope))
