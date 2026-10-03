"""输出字节日志 —— 会话输出的唯一真源。

offset 从 0 起、单调递增、永不回退；区间语义为半开 `[start, end)`。
"""

from __future__ import annotations

from dataclasses import dataclass

from . import scan
from .errors import OffsetAhead


@dataclass(frozen=True, slots=True)
class Resume:
    """从 `from_offset` 起补齐即可。"""

    from_offset: int


@dataclass(frozen=True, slots=True)
class Rebuild:
    """客户端断点早于日志保留区，只能走模型快照重建。"""


class OutputJournal:
    """追加式字节日志。"""

    def __init__(self, budget_bytes: int) -> None:
        if budget_bytes <= 0:
            raise ValueError("budget_bytes 必须为正")
        self._budget = budget_bytes
        self._buf = bytearray()
        self._base = 0  # _buf[0] 对应的绝对 offset

    @property
    def start_offset(self) -> int:
        """保留区间的起点（裁剪后 > 0）。"""
        return self._base

    @property
    def end_offset(self) -> int:
        """已追加字节的绝对末尾。"""
        return self._base + len(self._buf)

    def append(self, data: bytes) -> None:
        if data:
            self._buf.extend(data)

    def trim_to_budget(self) -> int:
        """按预算裁剪头部，返回裁剪掉的字节数。

        裁剪点由 `scan.trim_cut_offset` 给出：≥ 超出量的某个干净边界，**不保证最小**。
        不切开转义序列才是硬要求——从序列中间切断再重放会产生可见乱码。
        """
        excess = len(self._buf) - self._budget
        if excess <= 0:
            return 0
        cut = scan.trim_cut_offset(self._buf, excess)
        del self._buf[:cut]
        self._base += cut
        return cut

    def read(self, from_offset: int, length: int | None = None) -> bytes:
        """读取 `[from_offset, ...)`；起点早于保留区时抬到保留区起点。

        这个抬升是**静默**的：调用方若可能拿着已裁剪的游标，必须先问 `plan_attach`，
        否则会拿到半截内容而无人察觉。
        """
        begin = max(from_offset, self._base) - self._base
        if begin >= len(self._buf):
            return b""
        if length is None:
            return bytes(self._buf[begin:])
        return bytes(self._buf[begin : begin + max(0, length)])


def plan_attach(journal: OutputJournal, cursor: int | None) -> Resume | Rebuild:
    """纯函数：给定客户端游标，决定续传还是重建。

    - `cursor is None`：全新订阅者，语义上就是游标 0——同样受裁剪边界约束。
    - 游标在保留区内：续传。
    - 游标早于保留区：重建（唯一的有损路径）。
    - 游标超前于末尾：抛 `OffsetAhead`（协议不一致，不静默当作全新客户端）。
    """
    if cursor is None:
        cursor = 0
    if cursor > journal.end_offset:
        raise OffsetAhead(f"客户端游标 {cursor} 超前于日志末尾 {journal.end_offset}")
    if cursor < journal.start_offset:
        return Rebuild()
    return Resume(cursor)
