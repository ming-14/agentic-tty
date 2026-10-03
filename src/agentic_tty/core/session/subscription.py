"""订阅者游标：从字节日志补齐，**不认识订阅者**。

一个订阅者持有"它拿到哪了"（`next_offset`），核心层按这个游标从日志切片。
对齐决策（`Resume` / `Rebuild`）在构造时做一次——游标落后到已裁剪区间就**重同步**：
终端会话能从终端模型重建完整快照，子进程会话没有模型、被裁掉的字节找不回来
（此时 `lossy` 为真，只能从保留区起点给）。

**谁在听、推到哪、怎么限流**都不在这里：订阅表、出站队列、ack 窗口绑着外部连接，
属于消费者（见架构设计 §4.6 的边界）。
"""

from __future__ import annotations

from ..errors import CoreError, OffsetTrimmed
from ..journal import Rebuild
from ..ports import Stream
from .base import Session


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

    def pull(self) -> bytes:
        """取"游标之后到日志末尾"的字节；没有新数据返回空。

        构造时判定要重建的，第一次 `pull()` 先吐重建字节。

        构造**之后**日志又被裁（订阅者太慢）时抛 `OffsetTrimmed`——`journal.read()`
        对落后游标是静默抬升的，不查这一下就会丢字节而无人察觉（见架构设计 §4.4）。
        """
        if self._pending:
            data, self._pending = self._pending, b""
            return data
        journal = self._session.journal_for(self._stream)
        if self._next < journal.start_offset:
            raise OffsetTrimmed(
                f"订阅游标 {self._next} 已被裁剪（保留区自 {journal.start_offset} 起）"
            )
        data = journal.read(self._next)
        self._next += len(data)
        return data
