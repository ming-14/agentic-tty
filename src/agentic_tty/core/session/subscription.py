"""订阅者游标：从字节日志补齐，**不认识订阅者**。

一个订阅者持有"它拿到哪了"（`next_offset`），核心层按这个游标从日志切片。
对齐决策（`Resume` / `Rebuild`）在构造时做一次——游标落后到已裁剪区间就**重同步**：
终端会话能从终端模型重建完整快照，子进程会话没有模型、被裁掉的字节找不回来
（此时 `lossy` 为真，只能从保留区起点给）。

拉取带**批量上限**：不设上限就是一次把日志尾部整段复制出来（预算默认 8 MiB），慢
消费者要么一口吃下、要么自己切片，而内存里已经先复制了一份——"每轮按预算处理"也就
落不了地。

尺寸变更（`ResizeEvent`）与字节共用同一个 offset 空间，随 `Pull` 一起交出：订阅者据此
在字节流里插帧，否则落后的 raw 订阅者会拿新尺寸解释旧字节。

**谁在听、推到哪、怎么限流**都不在这里：订阅表、出站队列、ack 窗口绑着外部连接，
属于消费者（见架构设计 §4.6 的边界）。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..errors import CoreError, OffsetTrimmed
from ..journal import Rebuild
from ..ports import Stream
from .base import ResizeEvent, Session


@dataclass(frozen=True, slots=True)
class Pull:
    """一次拉取的结果。

    `start` 是这段字节在日志里的起点（走重建时是快照对齐到的那个 offset）。
    `resizes[i].offset` 落在 `[start, start + len(data))` 内：该 offset 起（含）的字节
    按新尺寸解释。
    """

    start: int
    data: bytes = b""
    resizes: tuple[ResizeEvent, ...] = ()


class Subscription:
    """一个订阅者的游标。"""

    def __init__(
        self,
        session: Session,
        stream: Stream = Stream.STDOUT,
        cursor: int | None = None,
    ) -> None:
        self._session = session
        self._stream = stream
        self._lossy = False
        plan = session.attach_plan(cursor, stream)
        if isinstance(plan, Rebuild):
            # 游标落到裁剪区间：只能重同步
            self._pending = self._resync(session, stream)
            self._next = session.journal_for(stream).end_offset
        else:
            self._pending = b""
            self._next = plan.from_offset

    @property
    def lossy(self) -> bool:
        """本次对齐是否丢了内容。

        终端会话能从终端模型重建完整快照，不丢；子进程会话的字节流就是唯一真源，
        被裁掉的那段找不回来——只能从保留区起点给，**这一段是丢了的**。
        """
        return self._lossy

    def _resync(self, session: Session, stream: Stream) -> bytes:
        try:
            return session.rebuild_bytes()
        except CoreError:
            journal = session.journal_for(stream)
            self._lossy = True
            return journal.read(journal.start_offset)

    @property
    def next_offset(self) -> int:
        """下一个要推给这个订阅者的字节偏移。"""
        return self._next

    def pull(self, max_bytes: int | None = None) -> Pull:
        """取"游标之后到日志末尾"的至多 `max_bytes` 字节；没有新数据时 `data` 为空。

        设了 `max_bytes` 就是分片拉取：剩下的留在日志里，下次 `pull()` 接着给。
        构造时判定要重建的，第一次 `pull()` 先吐重建字节（同样受 `max_bytes` 约束）。

        构造**之后**日志又被裁（订阅者太慢）时抛 `OffsetTrimmed`——`journal.read()`
        对落后游标是静默抬升的，不查这一下就会丢字节而无人察觉（见架构设计 §4.4）。
        """
        if self._pending:
            return Pull(start=self._next, data=self._take_pending(max_bytes))
        journal = self._session.journal_for(self._stream)
        if self._next < journal.start_offset:
            raise OffsetTrimmed(
                f"订阅游标 {self._next} 已被裁剪（保留区自 {journal.start_offset} 起）"
            )
        start = self._next
        data = journal.read(start, max_bytes)
        self._next += len(data)
        return Pull(start=start, data=data, resizes=self._resizes_in(start, self._next))

    def _take_pending(self, max_bytes: int | None) -> bytes:
        if max_bytes is None:
            data, self._pending = self._pending, b""
            return data
        data, self._pending = self._pending[:max_bytes], self._pending[max_bytes:]
        return data

    def _resizes_in(self, start: int, end: int) -> tuple[ResizeEvent, ...]:
        return tuple(e for e in self._session.resize_events(start) if e.offset < end)
