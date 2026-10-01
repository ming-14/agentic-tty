"""把字节连接包成"收发帧"。

帧格式在 `protocol`、连接在 `transport`，这里是两者之间唯一那层胶水。有了它，
`daemon` 与客户端都只说"收一个信封 / 发一个信封"，不碰 `socket`，也不碰 `struct`。

**收发允许分属两个线程**（读线程只调 `recv`、写线程只调 `send`），这是刻意的：
写会在对端不读时阻塞，压在事件循环上会冻住整个进程。
"""

from __future__ import annotations

from ..protocol.envelope import Envelope, from_json, to_json
from ..protocol.frame import BytesFrame, ControlFrame, FrameDecoder, encode_bytes, encode_control
from .stream import Connection


class Channel:
    """一条连接上的帧收发。"""

    def __init__(self, connection: Connection) -> None:
        self._connection = connection
        self._decoder = FrameDecoder()

    @property
    def peer(self) -> str:
        return self._connection.peer

    def recv(self, timeout: float | None = 0.2) -> list[ControlFrame | BytesFrame]:
        """读一轮，返回其中已经完整的帧（可能为空）。"""
        data = self._connection.recv(timeout=timeout)
        return self._decoder.feed(data) if data else []

    def send(self, envelope: Envelope) -> None:
        """发一条控制帧（信封）。"""
        self._connection.send(encode_control(to_json(envelope)))

    def send_bytes(self, stream: str, key: str, data: bytes) -> None:
        """发一条字节帧。"""
        self._connection.send(encode_bytes(stream, key, data))

    def close(self) -> None:
        self._connection.close()


def decode_control(frame: ControlFrame) -> Envelope:
    """控制帧 → 信封。"""
    return from_json(frame.data)
