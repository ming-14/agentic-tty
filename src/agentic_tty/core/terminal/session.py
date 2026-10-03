"""Pty 会话：单流 + 终端模型。

与子进程会话的差异只有两处：摄入时要喂终端模型、只有一路输出。
"""

from __future__ import annotations

from ...foundation.logs import get_logger
from ..errors import CoreError
from ..ports import HostFactory, HostMetadata, SessionSpec, Stream, TerminalHost
from ..session.base import Session

_logger = get_logger("core.terminal.session")


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
        _logger.info("会话尺寸已变更 uid=%s -> %dx%d", self.uid, cols, rows)

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
