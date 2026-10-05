"""Pty 会话：单流 + 终端模型。

与子进程会话的差异只有两处：摄入时要喂终端模型、只有一路输出。
"""

from __future__ import annotations

from bisect import bisect_left

from ...foundation.logs import get_logger
from ..errors import CoreError
from ..ports import HostFactory, HostMetadata, SessionSpec, Stream, TerminalHost
from ..session.base import ResizeEvent, Session

_logger = get_logger("core.terminal.session")


def _event_offset(event: ResizeEvent) -> int:
    """`bisect` 的键：按 offset 定界，别每次重建一份 offset 列表。"""
    return event.offset


class TerminalSession(Session):
    """伪终端会话。"""

    def __init__(
        self,
        uid: str,
        spec: SessionSpec,
        host_factory: HostFactory,
        *,
        journal_budget_bytes: int,
    ) -> None:
        super().__init__(uid, spec, host_factory, journal_budget_bytes=journal_budget_bytes)
        self._cols = spec.cols
        self._rows = spec.rows
        # 尺寸变更史（按 offset 升序）：订阅者靠它在字节流里插帧，见 `resize_events`。
        # 与日志同进退（见 `_prune_resizes`），不随会话时长无限增长。
        self._resizes: list[ResizeEvent] = []
        # 保留区起点那一刻的尺寸。事件被裁掉后，"那段字节按多大解释"仍答得出——
        # 否则重同步 / 新订阅者只有快照、不知道基线尺寸（见 `size_at`）。
        self._baseline = ResizeEvent(0, spec.cols, spec.rows)

    @property
    def cols(self) -> int:
        return self._cols

    @property
    def rows(self) -> int:
        return self._rows

    def resize(self, cols: int, rows: int) -> None:
        """改尺寸。**只允许所有者线程调用**（模型与 PTY 必须同一次调用内都改掉）。"""
        self._terminal_host().resize(cols, rows)
        self._cols = cols
        self._rows = rows
        # 记在**字节 offset 空间**里：此刻日志末尾之后的字节按新尺寸解释。
        self._resizes.append(ResizeEvent(self.journal.end_offset, cols, rows))
        _logger.info("会话尺寸已变更 uid=%s -> %dx%d", self.uid, cols, rows)

    def resize_events(self, since: int = 0) -> tuple[ResizeEvent, ...]:
        """尺寸变更事件（按 offset 升序，只给 `offset >= since` 的）。

        已裁剪出保留区的不再返回——那时的尺寸由 `size_at` 答（见架构设计 §4.4）。
        """
        index = bisect_left(self._resizes, since, key=_event_offset)
        return tuple(self._resizes[index:])

    def size_at(self, offset: int) -> tuple[int, int]:
        """`offset` 处的字节按多大解释：`offset` 起生效的那次变更，没有就是基线。

        `offset` 早于保留区起点时，答案仍是保留区起点那一刻的尺寸——**不早于保留区的
        字节本就读不到**，所以问它们没有意义。
        """
        # 最后一个 `offset' <= offset` 的变更；没有就落到基线。
        index = bisect_left(self._resizes, offset + 1, key=_event_offset) - 1
        if index < 0:
            return self._baseline.cols, self._baseline.rows
        event = self._resizes[index]
        return event.cols, event.rows

    def _prune_resizes(self) -> None:
        """丢掉已裁剪出保留区的尺寸变更，只留 `offset >= 保留区起点` 的。

        丢掉之前先把它压成基线：保留区起点那一刻的尺寸，供 `size_at` 作答。
        """
        start = self.journal.start_offset
        index = bisect_left(self._resizes, start, key=_event_offset)
        if index > 0:
            self._baseline = self._resizes[index - 1]
            del self._resizes[:index]

    def _journal_trimmed(self, stream: Stream) -> None:
        self._prune_resizes()

    def rebuild_bytes(self) -> bytes:
        """重建字节（RIS + 模式恢复 + scrollback + 可见区）。"""
        return self._terminal_host().rebuild_bytes()

    def screen_text(self) -> str:
        """可见屏幕纯文本。"""
        return self._terminal_host().screen_text()

    def full_text(self) -> str:
        """含滚动历史的可见文本。"""
        return self._terminal_host().full_text()

    def screen_cells(self) -> tuple[tuple[str, ...], ...]:
        """可见屏幕字符格栅。"""
        return self._terminal_host().screen_cells()

    def render_svg(self) -> str:
        """可见屏幕的 SVG。"""
        return self._terminal_host().render_svg()

    def render_image(self, *, scale: float = 1.0, fmt: str = "png") -> bytes:
        """可见屏幕的位图。"""
        return self._terminal_host().render_image(scale=scale, fmt=fmt)

    def metadata(self) -> HostMetadata:
        return self._terminal_host().metadata()

    def _feed_model(self, data: bytes, stream: Stream) -> bytes:
        return self._terminal_host().ingest(data)

    def _terminal_host(self) -> TerminalHost:
        host = self.host
        if host is None:
            raise CoreError("会话未启动")
        # 宿主由工厂按 mode 产出：pty 标签只配得到 TerminalHost（见 host_factory）
        return host  # type: ignore[return-value]
