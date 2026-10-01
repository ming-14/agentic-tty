"""输出字节日志 —— 会话输出的唯一真源。

- offset 从 0 起、单调递增、永不回退；区间语义为半开 `[start, end)`。
- 超预算时从头裁剪，**裁剪点对齐转义序列边界**：从序列中间切断再重放，
  会产生可见乱码。
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

    reason: str


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

    def __len__(self) -> int:
        return len(self._buf)

    def append(self, data: bytes) -> None:
        if data:
            self._buf.extend(data)

    def trim_to_budget(self) -> int:
        """按预算裁剪头部，返回裁剪掉的字节数。

        裁剪点由 `scan.clean_offset_at_or_after` 对齐到转义序列/字符边界。
        扫描从缓冲起点开始，是 O(n)；裁剪是低频路径，暂不做增量扫描状态。
        """
        excess = len(self._buf) - self._budget
        if excess <= 0:
            return 0
        cut = scan.clean_offset_at_or_after(self._buf, excess)
        if cut <= 0:
            return 0
        del self._buf[:cut]
        self._base += cut
        return cut

    def read(self, from_offset: int, length: int | None = None) -> bytes:
        """读取 `[from_offset, ...)`；起点早于保留区时自动抬到保留区起点。"""
        begin = max(from_offset, self._base) - self._base
        if begin >= len(self._buf):
            return b""
        if length is None:
            return bytes(self._buf[begin:])
        return bytes(self._buf[begin : begin + max(0, length)])

    def replay_offset(self) -> int:
        """重建对齐点：尾部残缺序列/字符之前的最后一个干净边界。"""
        return self._base + scan.replay_offset(self._buf)

    def clean_offset_at_or_after(self, offset: int) -> int:
        """把绝对 offset 抬到 >= 它的最小干净边界。"""
        if offset <= self._base:
            return self._base
        return self._base + scan.clean_offset_at_or_after(self._buf, offset - self._base)


def plan_attach(journal: OutputJournal, cursor: int | None, end: int) -> Resume | Rebuild:
    """纯函数：给定客户端游标与日志末尾，决定续传还是重建。

    - `cursor is None`：全新订阅者，语义上就是游标 0——因此同样受裁剪边界约束。
    - 游标在保留区内：续传。
    - 游标早于保留区：重建（唯一的有损路径）。
    - 游标超前于末尾：抛 `OffsetAhead`（协议不一致，不静默当作全新客户端）。

    全新订阅者不能直接给 `Resume(0)`：日志裁剪过之后 0 已不在保留区，
    `journal.read(0)` 会静默抬到保留区起点，订阅者拿到半截内容而无人察觉。
    """
    if cursor is None:
        cursor = 0
    if cursor > end:
        raise OffsetAhead(f"客户端游标 {cursor} 超前于日志末尾 {end}")
    if cursor < journal.start_offset:
        return Rebuild("trimmed")
    return Resume(cursor)
