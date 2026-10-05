"""输入队列：按**字节**计量的写队列（唯一写者消费）。

按块数计量等于没有上限——一块可以任意大（粘贴一整篇文本就是一块），所以这里按字节算，
并给两个水位：

- 越过**软水位**：字节照收，但调用方应让发送方本端排队（消费者据此下发 hold 信号）。
- 越过**硬上限**：拒收（`REJECTED`），调用方按违约处理（消费者据此断开那条连接）。

**"给谁 Hold、断哪条连接"绑着外部连接，属于消费者**——这里只出机制（见架构设计 §12）。
"""

from __future__ import annotations

import threading
from collections import deque
from enum import StrEnum


class InputVerdict(StrEnum):
    """一次输入投递的判定。"""

    QUEUED = "queued"
    """已收下，且队列还在软水位以下。"""
    HOLD = "hold"
    """已收下，但队列越过了软水位——调用方应让发送方本端排队。"""
    REJECTED = "rejected"
    """超过硬上限，未收下——调用方按违约处理。"""


class WriteQueue:
    """按字节计量的输入队列；单写者消费。"""

    def __init__(self, *, max_bytes: int, high_watermark: int, low_watermark: int) -> None:
        if not 0 < low_watermark <= high_watermark <= max_bytes:
            raise ValueError("水位必须满足 0 < low ≤ high ≤ max_bytes")
        self._max_bytes = max_bytes
        self._high = high_watermark
        self._low = low_watermark
        self._items: deque[bytes] = deque()
        self._bytes = 0
        self._cv = threading.Condition()
        # 回落到低水位（或更低）时置位、越过高水位时清除——两个水位之间保持原状，
        # 这样 Hold 与放行之间有迟滞，不会在水位线上反复抖动。
        self._drained = threading.Event()
        self._drained.set()

    @property
    def depth_bytes(self) -> int:
        """当前积压字节数。"""
        return self._bytes

    @property
    def drained(self) -> bool:
        """队列是否已回落到低水位（可放行）；越过高水位则转为 False。"""
        return self._drained.is_set()

    def wait_drained(self, timeout: float | None = None) -> bool:
        """阻塞等到队列回落到低水位（放行发送方的时机）。"""
        return self._drained.wait(timeout)

    def put(self, data: bytes) -> InputVerdict:
        """入队一段字节；超过硬上限则整块拒收（不收半个）。"""
        if not data:
            return InputVerdict.QUEUED if self._drained.is_set() else InputVerdict.HOLD
        with self._cv:
            if self._bytes + len(data) > self._max_bytes:
                return InputVerdict.REJECTED
            self._items.append(data)
            self._bytes += len(data)
            over = self._bytes > self._high
            if over:
                self._drained.clear()
            self._cv.notify()
            return InputVerdict.HOLD if over else InputVerdict.QUEUED

    def get(self, timeout: float) -> bytes | None:
        """取一段字节；超时返回 None。"""
        with self._cv:
            if not self._items:
                self._cv.wait(timeout)
            if not self._items:
                return None
            data = self._items.popleft()
            self._bytes -= len(data)
            if self._bytes <= self._low:
                self._drained.set()
            return data
