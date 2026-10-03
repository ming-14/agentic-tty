"""会话运行时驱动：读线程 → 有界桥 → 所有者侧摄入；写线程是唯一写者。

`pump()` 必须由所有者线程调用（排空桥 → `ingest_stream` → `refresh`），会话状态
因此仍只在所有者线程上改动。写线程独占 `send()`：`Pty.write` 在缓冲写满时会阻塞，
压在事件循环（或 UI 线程）上会冻住整个进程。
"""

from __future__ import annotations

import queue
import threading
import time
from collections import deque

from ...foundation.logs import get_logger
from ..session.base import Session
from ..session.state import SessionState
from .bridge import ThreadBridge
from .reader import StreamReader

_logger = get_logger("core.runtime.runner")


class SessionRunner:
    """一个会话的运行时驱动。"""

    def __init__(
        self,
        session: Session,
        *,
        bridge_maxsize: int = 256,
        read_timeout: float = 0.2,
        max_bytes: int = 65536,
        drain_limit: int = 128,
        write_queue_maxsize: int = 64,
    ) -> None:
        self._session = session
        self._bridge = ThreadBridge(bridge_maxsize)
        self._readers: list[StreamReader] = []
        self._read_timeout = read_timeout
        self._max_bytes = max_bytes
        self._drain_limit = drain_limit
        self._write_queue: queue.Queue[bytes] = queue.Queue(write_queue_maxsize)
        # 模型应答（DSR / 焦点应答等）：必须无条件回写，否则模型永远收不到回复而卡死。
        # 它单列一条无界通道，不受客户端输入队列的水位影响。
        self._urgent: deque[bytes] = deque()
        self._writer: threading.Thread | None = None
        self._writer_stop = threading.Event()

    @property
    def session(self) -> Session:
        return self._session

    @property
    def bridge(self) -> ThreadBridge:
        return self._bridge

    def start(self) -> None:
        """起读线程与写线程。"""
        self._session.expect_eof()  # 由本驱动负责读空，退出要等所有流 EOF
        for stream in self._session.streams():
            reader = StreamReader(
                self._session,
                self._bridge,
                stream,
                max_bytes=self._max_bytes,
                read_timeout=self._read_timeout,
            )
            reader.start()
            self._readers.append(reader)
        self._writer = threading.Thread(
            target=self._write_loop, name=f"writer-{self._session.uid[:8]}", daemon=True
        )
        self._writer.start()

    def pump(self) -> bool:
        """所有者线程调用：排空桥并推进退出检测。返回会话是否已结束（drained）。"""
        session = self._session
        for chunk in self._bridge.drain(self._drain_limit):
            if chunk.eof:
                session.mark_eof(chunk.stream)
            elif session.state in (SessionState.RUNNING, SessionState.EXITED):
                result = session.ingest_stream(chunk.stream, chunk.data)
                if result.response:
                    self._urgent.append(result.response)
        session.refresh()
        return session.drained

    def submit_input(self, data: bytes) -> bool:
        """把输入交给写线程（唯一写者）。队列满返回 False（由调用方决定怎么办）。"""
        if not data:
            return True
        try:
            self._write_queue.put_nowait(data)
            return True
        except queue.Full:
            _logger.warning("输入队列已满，丢弃本次输入 uid=%s", self._session.uid)
            return False

    def stop(self, timeout: float = 2.0) -> None:
        """停写线程与读线程。"""
        self._writer_stop.set()
        if self._writer is not None:
            self._writer.join(timeout)
            self._writer = None
        for reader in self._readers:
            reader.stop(timeout)
        self._readers.clear()

    # ── 便利循环（给没有自己事件循环的调用方用） ────────────────

    def run_until_drained(self, deadline: float, *, interval: float = 0.01) -> bool:
        while time.monotonic() < deadline:
            if self.pump():
                return True
            time.sleep(interval)
        return False

    def run_for(self, seconds: float, *, interval: float = 0.01) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self.pump():
                return
            time.sleep(interval)

    # ── 内部 ───────────────────────────────────────────────────

    def _write_loop(self) -> None:
        session = self._session
        while not self._writer_stop.is_set():
            data = self._take_write()
            if data is None:
                continue
            try:
                session.send(data)  # 阻塞写在写线程上，不挡所有者线程
            except Exception as exc:  # 宿主已关闭/被杀：正常终止路径
                if not self._writer_stop.is_set():
                    _logger.debug("写线程退出 uid=%s: %s", session.uid, exc)
                return

    def _take_write(self) -> bytes | None:
        """模型应答优先于客户端输入；两者都没有就等一小会。"""
        if self._urgent:
            return self._urgent.popleft()
        try:
            return self._write_queue.get(timeout=0.1)
        except queue.Empty:
            return None
