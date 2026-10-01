"""一条客户端连接：读线程 + 写线程 + 两个有界队列。

**读线程只做 `recv`，写线程只做 `send`**——写会在对端不读时阻塞，压在所有者循环上
会冻住整个进程。两个队列都有界：

- 入站满 → 读线程等待 → 不再从 socket 读 → 对端的 TCP 发送缓冲填满 → **对端写阻塞**。
  背压一路传回去，这里不无限缓冲。
- 出站满 → 说明这个客户端连自己的响应都不读了，按违约处理（调用方断开它）。

连接断开**不影响会话**：它只让这条连接上的请求失去归属，会话照常活着。
"""

from __future__ import annotations

import queue
import threading
import time

from ..foundation.logs import get_logger
from ..protocol.errors import ProtocolError
from ..protocol.frame import BytesFrame, ControlFrame
from ..transport.channel import Channel
from ..transport.errors import ConnectionClosed
from ..transport.stream import Connection
from .handler import Reply

_logger = get_logger("daemon.connection")

_POLL_INTERVAL = 0.1
"""队列空转时的等待步长；也是"停止请求"的响应粒度。"""


class ClientConnection:
    """一条接入的连接。"""

    def __init__(
        self,
        connection: Connection,
        *,
        inbound_maxsize: int,
        outbound_maxsize: int,
        read_timeout: float = 0.2,
    ) -> None:
        self._channel = Channel(connection)
        self._inbound: queue.Queue[ControlFrame | BytesFrame] = queue.Queue(inbound_maxsize)
        self._outbound: queue.Queue[Reply] = queue.Queue(outbound_maxsize)
        self._stop = threading.Event()
        self._closed = threading.Event()
        self._read_timeout = read_timeout
        self._threads: list[threading.Thread] = []

    @property
    def peer(self) -> str:
        return self._channel.peer

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    # ════════════════════════════════════════════════════════════
    # 所有者线程侧
    # ════════════════════════════════════════════════════════════

    def start(self) -> None:
        """起读、写两个线程。"""
        self._threads = [
            threading.Thread(target=self._read_loop, name=f"rx-{self.peer}", daemon=True),
            threading.Thread(target=self._write_loop, name=f"tx-{self.peer}", daemon=True),
        ]
        for thread in self._threads:
            thread.start()

    def take_inbound(self, limit: int) -> list[ControlFrame | BytesFrame]:
        """非阻塞取走至多 `limit` 帧（每轮有额度，不让一条连接独占一轮）。"""
        frames: list[ControlFrame | BytesFrame] = []
        for _ in range(limit):
            try:
                frames.append(self._inbound.get_nowait())
            except queue.Empty:
                break
        return frames

    def submit(self, reply: Reply) -> bool:
        """投递一条响应；返回 False 表示这个客户端已经不读了。"""
        if self._closed.is_set():
            return False
        try:
            self._outbound.put_nowait(reply)
            return True
        except queue.Full:
            return False

    def close(self) -> None:
        """标记关闭并断开底层连接（让阻塞在收发的线程醒过来）。幂等。"""
        if self._closed.is_set():
            return
        self._closed.set()
        self._stop.set()
        self._channel.close()

    def join(self, timeout: float) -> bool:
        """等两个线程收工；返回是否都停了。"""
        deadline = time.monotonic() + timeout
        for thread in list(self._threads):
            thread.join(max(0.0, deadline - time.monotonic()))
        self._threads = [t for t in self._threads if t.is_alive()]
        return not self._threads

    # ════════════════════════════════════════════════════════════
    # 线程体
    # ════════════════════════════════════════════════════════════

    def _read_loop(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    frames = self._channel.recv(self._read_timeout)
                except ConnectionClosed as exc:
                    _logger.debug("读线程收工 peer=%s: %s", self.peer, exc)
                    return
                except ProtocolError as exc:
                    # 字节不是合法帧：字节流已经错位，接着解只会解出垃圾，只能断。
                    _logger.warning("peer=%s 送来不合法帧，断开: %s", self.peer, exc)
                    return
                for frame in frames:
                    if not self._offer(frame):
                        return
        except Exception:
            # 线程体不能让异常逃出去：逃出去只会被解释器打到 stderr，而连接看起来
            # 还活着，所有者线程会一直往一个死掉的连接投响应。
            _logger.exception("读线程异常退出 peer=%s", self.peer)
        finally:
            # **任一头收工就整条收掉**：只标记自己不关 socket 的话，另一个线程还在
            # 原地等，对端也永远看不到 EOF。
            self.close()

    def _write_loop(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    reply = self._outbound.get(timeout=_POLL_INTERVAL)
                except queue.Empty:
                    continue
                try:
                    self._channel.send(reply.envelope)
                    if reply.binary is not None:
                        self._channel.send_bytes(reply.stream, reply.envelope.mid, reply.binary)
                except ConnectionClosed as exc:
                    _logger.debug("写线程收工 peer=%s: %s", self.peer, exc)
                    return
        except Exception:
            _logger.exception("写线程异常退出 peer=%s", self.peer)
        finally:
            self.close()

    def _offer(self, frame: ControlFrame | BytesFrame) -> bool:
        """阻塞式投递入站帧；停止时放弃并返回 False。"""
        while not self._stop.is_set():
            try:
                self._inbound.put(frame, timeout=_POLL_INTERVAL)
                return True
            except queue.Full:
                continue
        return False
