"""pywezterm 宿主：PTY + 终端模型。

`pywezterm` 不在 PyPI 上、也不安装：它是仓库 `vendor/` 里的长期依赖，由
`core/runtime/vendor.py` 接进 `sys.path`。因此这里做**惰性导入**——真正需要 PTY
时才导入扩展，纯子进程场景不碰它。
"""

from __future__ import annotations

import os
import time
from types import ModuleType

from ...foundation.logs import get_logger
from ..ports import HostMetadata, SessionSpec, Stream
from .errors import DependencyMissing, HostSpawnError
from .process_tree import ProcessTree, close_job, create_job

_logger = get_logger("core.runtime.pty_host")

_pywezterm: ModuleType | None = None

# ConPTY 拿不到真 EOF：排空之后阻塞读永不返回，带超时的读只会等到超时返 b""，
# 所以"读空"与"排空"是两回事。退出信号（进程句柄）又比最后一段输出早约 15ms
# ——字节要经 conhost 中继，负载下能迟到 200ms 以上。于是排空只能按"退出之后
# 静默多久"判定：静默不够长就会把尾巴永久丢在管道里。
_DRAIN_QUIET = 0.3

# snapshot() 的 cell = (col, ch, fg, bg, bold, italic, underline, reverse, ?, width)：
# 它的 docstring 字段表漏了一项，按那张表数会数错格宽
_CELL_TEXT = 1
_CELL_WIDTH = 9

_TITLE_NOT_IMPLEMENTED = "（标题未实现）"
"""`Terminal.get_title()` 是哑接口：文档说读 OSC 0/2，实测恒返回它自己的默认值
（`'wezterm'`），流里发什么标题都不变。拿默认值冒充真标题会误导，所以显式标出来。"""


class _DrainQuiet:
    """按"最后一次读到字节"计静默：退出之后静默够了，才算这一路排空。"""

    def __init__(self, quiet: float) -> None:
        self._quiet = quiet
        self._since: float | None = None

    def note_data(self) -> None:
        self._since = None  # 来过字节：静默重新计

    def settled(self, exited: bool) -> bool:
        now = time.monotonic()
        if not exited:
            self._since = None
            return False
        if self._since is None:
            self._since = now
            return False
        return now - self._since >= self._quiet


def require_pywezterm() -> ModuleType:
    """惰性导入 pywezterm；不可用则抛 `DependencyMissing`。"""
    global _pywezterm
    if _pywezterm is None:
        from .vendor import ensure_vendor_on_path

        ensure_vendor_on_path()
        try:
            import pywezterm  # type: ignore[import-not-found]
        except Exception as exc:
            raise DependencyMissing(
                f"pywezterm 不可用（应在仓库 vendor/ 下，且不通过 pip 安装）: {exc}"
            ) from exc
        _pywezterm = pywezterm
    return _pywezterm


def _to_bytes(value) -> bytes:
    if not value:
        return b""
    return value.encode("utf-8") if isinstance(value, str) else bytes(value)


def _row_cells(row) -> tuple[str, ...]:
    """一行 snapshot → 字符格栅。

    `snapshot()` 只吐宽字符的首格——续格根本不出现，行尾的宽字符也一样，所以续格
    要按格宽自己补。行尾的续格照样截掉：另一后端（`localpty`）截的是行尾空白，
    续格是空串，在那边也属于被截之列。
    """
    cells: list[str] = []
    for cell in row:
        cells.append(cell[_CELL_TEXT])
        cells.extend("" for _ in range(max(0, cell[_CELL_WIDTH] - 1)))
    while cells and not cells[-1]:
        cells.pop()
    return tuple(cells)


class PtyHost:
    """伪终端宿主。"""

    def __init__(self, spec: SessionSpec) -> None:
        pw = require_pywezterm()
        self._pty = pw.Pty(cols=spec.cols, rows=spec.rows)
        self._term = pw.Terminal(cols=spec.cols, rows=spec.rows)
        env = dict(os.environ)
        env.update(spec.env)
        # 先建作业、再 spawn：子进程在 CreateProcessW 时就入作业，没有"创建后再赋值"
        # 的时间窗（那条窗口会让先 fork 出的孙进程逃出作业）
        job = create_job()
        try:
            pid, _ = self._pty.spawn(list(spec.argv), cwd=spec.cwd, env=env, job_handle=job)
        except Exception as exc:
            close_job(job)
            raise HostSpawnError(f"启动 PTY 失败 {list(spec.argv)}: {exc}") from exc
        self._tree = ProcessTree(pid, job)
        self._closed = False
        self._drain = _DrainQuiet(_DRAIN_QUIET)
        # 程序自己 `cd` 之后只有 OSC 7 知道；拿不到就退回"创建时的目录"
        self._fallback_cwd = os.path.abspath(spec.cwd) if spec.cwd else os.getcwd()

    # ── HostLifecycle ──────────────────────────────────────────

    @property
    def pid(self) -> int | None:
        return None if self._closed else self._pty.child_pid()

    def read(self, max_bytes: int = 65536, timeout: float | None = 0.2) -> bytes:
        data = self._pty.read(max_bytes, timeout=timeout)
        if data:
            self._drain.note_data()
        return data

    def write(self, data: bytes) -> None:
        if not self._closed:
            self._pty.write(data)

    def try_wait(self) -> int | None:
        return self._pty.try_wait()

    def poll_eof(self, stream: Stream = Stream.STDOUT) -> bool:
        """本路是否已排空：ConPTY 没有真 EOF，退出之后还要再静默一段才算。"""
        if self._closed:
            return True
        return self._drain.settled(self._pty.try_wait() is not None)

    def kill(self) -> None:
        self._tree.kill()

    def descendants(self) -> tuple[int, ...]:
        """本会话进程树的当前成员（不含根进程）。"""
        return self._tree.descendants()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._tree.close()
        try:
            self._pty.close()
        except Exception as exc:  # 句柄可能已失效；关闭失败不该中断生命周期
            _logger.debug("关闭 PTY 异常 uid=%s: %s", self._tree.pid, exc)

    # ── TerminalHost ───────────────────────────────────────────

    def ingest(self, data: bytes) -> bytes:
        """喂终端模型，返回模型要回写给应用的应答。"""
        self._term.feed(data)
        return self._term.drain_written()

    def resize(self, cols: int, rows: int) -> None:
        self._pty.resize(cols, rows)
        self._term.resize(cols, rows)

    def rebuild_bytes(self) -> bytes:
        """重建字节：RIS + 模式恢复 + scrollback + 可见区。"""
        parts = [b"\x1bc"]
        parts.append(_to_bytes(self._term.mode_restore_seq()))
        parts.append(_to_bytes(self._term.render_scrollback(keep_ansi=True)))
        parts.append(_to_bytes(self._term.render_ansi(include_cursor=True)))
        return b"".join(parts)

    def screen_text(self) -> str:
        """可见屏幕纯文本。"""
        return self._term.text()

    def full_text(self) -> str:
        """全量输出：历史区 + 可见区，用 `\\n` 连接。"""
        history = self._term.render_scrollback(keep_ansi=False)
        visible = self._term.text()
        if not history:
            return visible
        return f"{history}\n{visible}" if visible else history

    def screen_cells(self) -> tuple[tuple[str, ...], ...]:
        """可见屏幕字符格栅；宽字符的续格为空串。"""
        return tuple(_row_cells(row) for row in self._term.snapshot())

    def render_svg(self) -> str:
        """可见屏幕的 SVG。底层该参数必填，固定 0 = 不压缩。"""
        return self._term.render_svg(0)

    def render_image(self, *, scale: float = 1.0, fmt: str = "png") -> bytes:
        """可见屏幕的位图。"""
        return self._term.render_image(scale=scale, fmt=fmt)

    def metadata(self) -> HostMetadata:
        return HostMetadata(
            title=_TITLE_NOT_IMPLEMENTED,
            cwd=self._term.get_current_dir() or self._fallback_cwd,
        )
