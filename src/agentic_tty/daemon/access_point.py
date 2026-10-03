"""接入点：把线协议接进接缝。

守护进程对外的**唯一口子**——本机管道（`transport` 的 `pipe://`），**不是网络**：
不开 TCP 端口、外部不可达；一个名字上可以同时接多条连接，每条是独立的一条双向字节流。

它做四件事：挂监听、accept、按连接解帧、把答复按请求身份写回。

**接缝对它不透明**：它只认 `protocol` 的帧与信封，不认识 `sid`、等待引擎、订阅。

- 控制帧 → 解成 `Envelope`，包进 `WireRequest` 投 `on_request`。
- 字节帧 → `on_input(key, data)`。
- 答复 → `Envelope` 编回控制帧，`ByteChunk` 编回字节帧。

所有方法都**只在所有者线程上**调用。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from ..foundation.logs import get_logger
from ..protocol.envelope import Envelope, from_json, to_json
from ..protocol.errors import EnvelopeError, ProtocolError
from ..protocol.frame import (
    BytesFrame,
    ControlFrame,
    FrameReader,
    encode_bytes,
    encode_control,
)
from ..transport.errors import ConnectionClosed
from ..transport.pipe import PipeTransport
from ..transport.stream import Connection, Listener, parse_address
from .handler import Reply

_logger = get_logger("daemon.access_point")

_ACCEPT_BATCH = 8
"""每轮最多接受几条新连接。"""
_READ_BATCH = 8
"""每条连接每轮最多读几段。"""
_BUFFER = 1 << 16


@dataclass(frozen=True, slots=True)
class ByteChunk:
    """答复里的一段原始字节（会话输出 / 订阅推送）——以字节帧回给对端。

    `tag` 是流标签（取值见 `protocol.contracts.daemon_ipc`），`key` 是消息 `mid`。
    字节不走 JSON，否则每个字节都要 base64。
    """

    tag: int
    key: str
    data: bytes


@dataclass(frozen=True, slots=True)
class WireRequest:
    """接缝上的请求对象：一个信封 ＋ 它来的那条连接。

    **路由不靠 `id(request)`**：答复迟早会来，而请求对象一旦被回收，它的 id 就会被别的
    对象复用，答复就会回错连接。把连接直接挂在请求对象上，就不需要任何旁表——它活多久，
    路由就在多久。

    请求处理层只用 `envelope`；`connection` 是接入点自己的路由凭据。
    """

    envelope: Envelope
    connection: _Connection


def _encode_answer(answer: object) -> bytes:
    if isinstance(answer, Envelope):
        return encode_control(to_json(answer))
    if isinstance(answer, ByteChunk):
        return encode_bytes(answer.tag, answer.key, answer.data)
    # 答复类型不认识是**处理层的实现错误**，不是对端问题——抛 TypeError，
    # 让它在交答复那一层被记下来，别当成协议错误去关连接。
    raise TypeError(f"答复类型不认识: {type(answer).__name__}")


class _Connection:
    """一条连接：一个增量读帧器。"""

    def __init__(self, connection: Connection) -> None:
        self._connection = connection
        self._reader = FrameReader(lambda: connection.recv(_BUFFER, timeout=0))
        self.closed = False

    @property
    def peer(self) -> str:
        return self._connection.peer

    def read(self) -> list[ControlFrame | BytesFrame]:
        """读一轮。**读不到东西返回空**（含本轮无数据与"该流已作废"两种情形）。"""
        if self.closed:
            return []
        try:
            return self._reader.read()
        except (ConnectionClosed, ProtocolError) as exc:
            _logger.info("连接结束 peer=%s: %s", self.peer, exc)
            self.closed = True
            return []

    def send(self, data: bytes) -> None:
        self._connection.send(data)

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self._connection.close()


class AccessPoint:
    """本机接入点。"""

    def __init__(
        self,
        address: str,
        *,
        on_request: Callable[[object], object],
        on_input: Callable[[str, bytes], object],
    ) -> None:
        self._address = address
        self._on_request = on_request
        self._on_input = on_input
        self._listener: Listener | None = None
        self._connections: list[_Connection] = []

    @property
    def address(self) -> str:
        """实际挂上的地址。"""
        return str(self._listener.address) if self._listener is not None else self._address

    @property
    def connection_count(self) -> int:
        """当前接了几条连接（观测用）。"""
        return len(self._connections)

    def open(self) -> None:
        """挂监听。"""
        self._listener = PipeTransport().listen(parse_address(self._address))
        _logger.info("接入点已挂监听 %s", self._listener.address)

    def pump(self, timeout: float) -> None:
        """接受新连接（最多阻塞 `timeout`）＋ 每条连接读一轮。

        `accept` 兼作这一轮的"空闲等待"：没有新连接时阻塞到 `timeout`，有就立刻返回。
        """
        self._accept(timeout)
        for connection in list(self._connections):
            self._read(connection)
        self._reap()

    def send(self, reply: Reply) -> bool:
        """把答复写回它来的那条连接。

        不是本接入点发出的请求返回 `False`（调用方另找去处）；是则返回 `True`。
        """
        request = reply.request
        if not isinstance(request, WireRequest):
            return False
        connection = request.connection
        if connection.closed:
            return True
        try:
            connection.send(_encode_answer(reply.answer))
        except (ConnectionClosed, ProtocolError) as exc:
            _logger.warning("回写答复失败 peer=%s: %s", connection.peer, exc)
            connection.close()
        return True

    def stop_accepting(self) -> None:
        """关掉监听：不再接新连接。已接的连接照常收发——draining 期间答复还要回去。"""
        if self._listener is not None:
            self._listener.close()
            self._listener = None

    def close(self) -> None:
        """彻底关掉：监听 ＋ 所有连接。"""
        self.stop_accepting()
        for connection in self._connections:
            connection.close()
        self._connections.clear()

    # ════════════════════════════════════════════════════════════
    # 内部
    # ════════════════════════════════════════════════════════════

    def _accept(self, timeout: float) -> None:
        listener = self._listener
        if listener is None:
            return
        for index in range(_ACCEPT_BATCH):
            # 第一次等 `timeout`（兼作空闲等待），之后立刻返回——别把一轮的预算花光。
            try:
                connection = listener.accept(timeout=timeout if index == 0 else 0.0)
            except ConnectionClosed as exc:
                _logger.warning("接入点接受连接失败: %s", exc)
                return
            if connection is None:
                return
            self._connections.append(_Connection(connection))

    def _read(self, connection: _Connection) -> None:
        for _ in range(_READ_BATCH):
            frames = connection.read()
            if not frames:
                return
            for frame in frames:
                self._dispatch(connection, frame)
                if connection.closed:
                    return

    def _dispatch(self, connection: _Connection, frame: ControlFrame | BytesFrame) -> None:
        if isinstance(frame, BytesFrame):
            self._on_input(frame.key, frame.data)
            return
        try:
            envelope = from_json(frame.data)
        except EnvelopeError as exc:
            # 信封解不开：这条流作废（与 FrameReader 的"错位即作废"同一规矩）。
            _logger.warning("信封不合法 peer=%s: %s", connection.peer, exc)
            connection.close()
            return
        self._on_request(WireRequest(envelope, connection))

    def _reap(self) -> None:
        """摘掉已关的连接——否则连接表会一直涨。"""
        if all(not connection.closed for connection in self._connections):
            return
        self._connections = [
            connection for connection in self._connections if not connection.closed
        ]
