"""接入点：把线协议接进接缝。

本机管道（`transport` 的 `pipe://`）：一个名字上可以同时接多条连接，每条是独立的一条
双向字节流。挂监听、accept、按连接解帧、把答复按请求身份写回。

- 控制帧 → 解成 `Envelope`，包进 `WireRequest` 投 `on_request`。
- 字节帧 → `on_input(key, data)`。
- 答复 → `Envelope` 编回控制帧，`ByteChunk` 编回字节帧，**放进该连接的出站队列**。

**答复不在这里直接写**：写会在缓冲写满时阻塞，压在所有者线程上会冻住整个守护进程
（订阅推送尤其如此）。每连接一个按字节计量的**有界**出站队列，由**写线程**排空；
队列满就拒收（`send` 返回 `Delivery.CONGESTED`），调用方据此停掉该订阅的 `pull`——
**不阻塞、不丢字节**。

除写线程外，所有方法都**只在所有者线程上**调用。
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

from ..foundation.logs import get_logger
from ..protocol.contracts.daemon_ipc import STREAM_STDIN
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
from .handler import Delivery, Reply

_logger = get_logger("daemon.access_point")

_ACCEPT_BATCH = 8
"""每轮最多接受几条新连接。"""
_READ_BATCH = 8
"""每条连接每轮最多读几段。"""
_BUFFER = 1 << 16
_OUTBOX_BYTES = 1 << 20
"""每连接出站队列的字节上限——满了就拒收，让订阅的 `pull` 停下来。"""
_WRITER_JOIN = 2.0
"""收尾时等写线程收工的上限；它是守护线程，等不到也不挡进程退出。"""


@dataclass(frozen=True, slots=True)
class ByteChunk:
    """答复里的一段原始字节（会话输出 / 订阅推送）——以字节帧回给对端。

    `tag` 是流标签（取值见 `protocol.contracts.daemon_ipc`），`key` 是消息 `mid`。
    """

    tag: int
    key: str
    data: bytes


@dataclass(frozen=True, slots=True)
class WireRequest:
    """接缝上的请求对象：一个信封 ＋ 它来的那条连接。

    **路由不靠 `id(request)`**：请求对象一旦被回收，它的 id 就会被别的对象复用，
    答复就会回错连接。把连接直接挂在请求对象上，就不需要任何旁表。

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
    """一条连接：一个增量读帧器 ＋ 一个出站队列（写线程排空）。

    队列按**字节**计量、有上限：满了 `enqueue` 返回 False，调用方停掉该订阅的 `pull`——
    而不是在这里阻塞（会冻住所有者线程）或丢字节（订阅者会静默缺一段）。
    """

    def __init__(self, connection: Connection, *, outbox_bytes: int) -> None:
        self._connection = connection
        self._reader = FrameReader(lambda: connection.recv(_BUFFER, timeout=0))
        self._outbox: deque[bytes] = deque()
        self._outbox_bytes = 0
        self._limit = outbox_bytes
        self._outbox_cv = threading.Condition()
        self.closed = False
        self._writer = threading.Thread(
            target=self._write_loop, name=f"conn-writer-{connection.peer}", daemon=True
        )
        self._writer.start()

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
            # 走 `close()` 而不是只置标志：底层句柄（Windows HANDLE / POSIX fd）必须真的关掉。
            self.close()
            return []

    def enqueue(self, data: bytes) -> bool:
        """放进出站队列；没有空位返回 False（**不阻塞、不丢**）。

        队列为空时永远收下——否则一条本身就超过上限的答复会永远排不进去。
        """
        with self._outbox_cv:
            if self.closed:
                return False
            if self._outbox and self._outbox_bytes + len(data) > self._limit:
                return False
            self._outbox.append(data)
            self._outbox_bytes += len(data)
            self._outbox_cv.notify()
            return True

    def close(self) -> None:
        """关掉连接：叫停写线程并释放底层句柄。幂等，**任何线程都可调**。"""
        with self._outbox_cv:
            if self.closed:
                return
            self.closed = True
            self._outbox.clear()
            self._outbox_bytes = 0
            self._outbox_cv.notify_all()
        self._connection.close()

    def join_writer(self, timeout: float) -> None:
        self._writer.join(timeout)

    def _write_loop(self) -> None:
        """写线程：把队列里的字节按序写出。**宁可阻塞也不丢。**

        队首在**写完之前一直留在队列里**——正在写的那一帧也算在字节账上，否则写线程一
        取走队列就空了，上限形同虚设。
        """
        while True:
            with self._outbox_cv:
                while not self._outbox and not self.closed:
                    self._outbox_cv.wait()
                if not self._outbox:
                    return  # 只有"已关闭且队空"会走到这儿
                data = self._outbox[0]
            try:
                self._connection.send(data)
            except Exception as exc:  # 对端关了 / 句柄没了：正常终止路径
                _logger.info("写连接失败 peer=%s: %s", self.peer, exc)
                self.close()
                return
            with self._outbox_cv:
                if self._outbox and self._outbox[0] is data:
                    self._outbox.popleft()
                    self._outbox_bytes -= len(data)


class AccessPoint:
    """本机接入点。"""

    def __init__(
        self,
        address: str,
        *,
        on_request: Callable[[object], object],
        on_input: Callable[[str, bytes], object],
        outbox_bytes: int = _OUTBOX_BYTES,
    ) -> None:
        self._address = address
        self._on_request = on_request
        self._on_input = on_input
        self._outbox_bytes = outbox_bytes
        self._listener: Listener | None = None
        self._connections: list[_Connection] = []
        self._closed: list[_Connection] = []

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

    def send(self, reply: Reply) -> Delivery:
        """把答复写回它来的那条连接——**只入队**，真正写出在连接的写线程上。

        不是本接入点发出的请求返回 `NOT_MINE`（调用方另找去处）；连接堵住返回
        `CONGESTED`（没进队列）；连接没了返回 `GONE`。
        """
        request = reply.request
        if not isinstance(request, WireRequest):
            return Delivery.NOT_MINE
        connection = request.connection
        if connection.closed:
            return Delivery.GONE
        if not connection.enqueue(_encode_answer(reply.answer)):
            return Delivery.CONGESTED
        return Delivery.SENT

    def take_closed(self) -> list[object]:
        """取走"这一轮新关掉的连接"——**只在所有者线程上调**。

        处理层要靠它注销挂在这些连接上的订阅。关连接这件事也会发生在写线程上，所以
        不在那儿直接回调：处理层只许在所有者线程上被碰。
        """
        closed, self._closed = self._closed, []
        return list(closed)

    def stop_accepting(self) -> None:
        """关掉监听：不再接新连接。已接的连接照常收发——draining 期间答复还要回去。"""
        if self._listener is not None:
            self._listener.close()
            self._listener = None

    def close(self) -> None:
        """彻底关掉：监听 ＋ 所有连接（并等写线程收工）。"""
        self.stop_accepting()
        connections, self._connections = self._connections, []
        for connection in connections:
            connection.close()
        for connection in connections:
            connection.join_writer(_WRITER_JOIN)

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
            self._connections.append(_Connection(connection, outbox_bytes=self._outbox_bytes))

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
            if frame.tag != STREAM_STDIN:
                # 上行只认"输入"这一个标签；别的标签说明对端把下行帧发错了方向。
                _logger.warning("上行字节标签不合法 peer=%s: %#x", connection.peer, frame.tag)
                connection.close()
                return
            self._on_input(frame.key, frame.data)
            return
        try:
            envelope = from_json(frame.data)
        except EnvelopeError as exc:
            # 信封解不开 = 这条流作废（与 FrameReader"错位即作废"同一规矩）。
            _logger.warning("信封不合法 peer=%s: %s", connection.peer, exc)
            connection.close()
            return
        self._on_request(WireRequest(envelope, connection))

    def _reap(self) -> None:
        """摘掉已关的连接（并记下来，供处理层注销挂在上面的订阅）。"""
        if all(not connection.closed for connection in self._connections):
            return
        alive: list[_Connection] = []
        for connection in self._connections:
            if connection.closed:
                self._closed.append(connection)
            else:
                alive.append(connection)
        self._connections = alive
