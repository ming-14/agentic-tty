"""单会话驱动：读线程 → 有界桥 → 所有者侧摄入；写线程是唯一写者。

`pump()` 必须由所有者线程调用（排空桥 → `ingest_stream` → `refresh`），会话状态因此
仍只在所有者线程上改动。它交出**本轮事件清单**：消费者按会话 / 按流推送、按区间推进
订阅游标、感知退出，都靠它——不然只能每轮对所有会话 × 所有订阅全量试一遍。

写线程独占 `send()`：`Pty.write` 在缓冲写满时会阻塞，压在事件循环（或 UI 线程）上会
冻住整个进程。输入队列按**字节**计量（见 `input_queue`）。
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass

from ...foundation.logs import get_logger
from ..ports import Stream
from ..session.base import Session
from ..session.state import SessionState
from .bridge import ThreadBridge, Wakeup
from .input_queue import InputVerdict, WriteQueue
from .reader import StreamReader

_logger = get_logger("core.runtime.runner")


@dataclass(frozen=True, slots=True)
class Ingested:
    """某一路本轮进了 `[start, end)` 这段字节。"""

    stream: Stream
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class StreamEof:
    """某一路输出已排空。"""

    stream: Stream


@dataclass(frozen=True, slots=True)
class Exited:
    """会话本轮拿到了退出码。"""

    exit_code: int | None


PumpEvent = Ingested | StreamEof | Exited
"""`pump()` 交出的一轮事件。"""


class SessionRunner:
    """一个会话的运行时驱动。"""

    def __init__(
        self,
        session: Session,
        *,
        wakeup: Wakeup | None = None,
        bridge_maxsize: int = 256,
        read_timeout: float = 0.2,
        max_bytes: int = 65536,
        drain_limit: int = 128,
        input_max_bytes: int = 1 << 20,
        input_high_watermark: int = 1 << 18,
        input_low_watermark: int = 1 << 16,
    ) -> None:
        self._session = session
        self._bridge = ThreadBridge(bridge_maxsize)
        self._wakeup = wakeup
        self._readers: list[StreamReader] = []
        self._read_timeout = read_timeout
        self._max_bytes = max_bytes
        self._drain_limit = drain_limit
        self._write_queue = WriteQueue(
            max_bytes=input_max_bytes,
            high_watermark=input_high_watermark,
            low_watermark=input_low_watermark,
        )
        # 模型应答（DSR / 焦点应答等）：必须无条件回写，否则模型永远收不到回复而卡死。
        # 它单列一条无界通道，不受输入队列的水位影响。
        self._urgent: deque[bytes] = deque()
        self._writer: threading.Thread | None = None
        self._writer_stop = threading.Event()

    @property
    def session(self) -> Session:
        return self._session

    @property
    def bridge(self) -> ThreadBridge:
        return self._bridge

    @property
    def input_depth(self) -> int:
        """输入队列当前积压字节数（消费者据此决定要不要让发送方本端排队）。"""
        return self._write_queue.depth_bytes

    @property
    def input_drained(self) -> bool:
        """输入队列是否已回落到低水位（可放行）。"""
        return self._write_queue.drained

    def wait_input_drained(self, timeout: float | None = None) -> bool:
        """阻塞等到输入队列回落到低水位——放行发送方的时机。"""
        return self._write_queue.wait_drained(timeout)

    def start(self) -> None:
        """起读线程与写线程。"""
        self._session.expect_eof()  # 由本驱动负责读空，退出要等所有流 EOF
        for stream in self._session.streams():
            reader = StreamReader(
                self._session,
                self._bridge,
                stream,
                wakeup=self._wakeup,
                max_bytes=self._max_bytes,
                read_timeout=self._read_timeout,
            )
            reader.start()
            self._readers.append(reader)
        self._writer = threading.Thread(
            target=self._write_loop, name=f"writer-{self._session.uid[:8]}", daemon=True
        )
        self._writer.start()

    def pump(self) -> list[PumpEvent]:
        """所有者线程调用：排空桥、摄入、推进退出检测，返回**本轮事件清单**。"""
        session = self._session
        events: list[PumpEvent] = []
        for chunk in self._bridge.drain(self._drain_limit):
            if chunk.eof:
                session.mark_eof(chunk.stream)
                events.append(StreamEof(chunk.stream))
            elif session.state in (SessionState.RUNNING, SessionState.EXITED):
                result = session.ingest_stream(chunk.stream, chunk.data)
                events.append(Ingested(result.stream, result.start_offset, result.end_offset))
                if result.response:
                    self._urgent.append(result.response)
            else:
                # 会话已关闭：宿主已释放，这批字节无处可去。
                _logger.debug(
                    "丢弃已关闭会话的输出 uid=%s stream=%s bytes=%d",
                    session.uid,
                    chunk.stream,
                    len(chunk.data),
                )
        previous_exit = session.exit_code
        session.refresh()
        if previous_exit is None and session.exit_code is not None:
            events.append(Exited(session.exit_code))
        return events

    def submit_input(self, data: bytes) -> InputVerdict:
        """把输入交给写线程（唯一写者）。

        返回**判定**而不是布尔：`HOLD` = 已收下但队列越过了软水位（消费者应让发送方本端
        排队），`REJECTED` = 超过硬上限、**没有收下**（消费者按违约处理）。
        """
        return self._write_queue.put(data)

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
            self.pump()
            if self._session.drained:
                return True
            time.sleep(interval)
        return False

    def run_for(self, seconds: float, *, interval: float = 0.01) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.pump()
            if self._session.drained:
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
        return self._write_queue.get(timeout=0.1)
