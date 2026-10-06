"""`localpty` 宿主：平台 PTY 原语 + pyte 终端模型。

会话由 `ConsoleLauncher` 起——自带的那份在 `console.py`（Windows 走 `vendor/condrv`
的 ConDrv 直连，Linux 走 `vendor/openpty`），沙箱换掉同一个可调用对象即可复用本类
的全部终端侧逻辑。**不碰 pywezterm**——这个后端的存在就是为了不依赖那份编译产物。

原语是惰性导入的：纯 `pty` / `subprocess` 场景不会把它们拖进来。
"""

from __future__ import annotations

import os

from ....foundation.logs import get_logger
from ...ports import HostMetadata, SessionSpec, Stream
from .console import ConsoleLauncher, PtyPrimitive, open_console
from .screen import PyteScreen
from .svg import render_image, render_svg

_logger = get_logger("core.runtime.local_pty.host")


class LocalPtyHost:
    """伪终端宿主：平台原语负责 PTY，pyte 负责终端模型。"""

    def __init__(self, spec: SessionSpec, *, launch: ConsoleLauncher = open_console) -> None:
        console = launch(spec)
        self._pty: PtyPrimitive = console.pty
        self._pid = console.pid
        self._tree = console.tree
        self._screen = PyteScreen(spec.cols, spec.rows)
        self._closed = False
        # pyte 只认 OSC 0/2 的标题，不认 OSC 7，所以拿不到程序自己 cd 之后的位置
        self._fallback_cwd = os.path.abspath(spec.cwd) if spec.cwd else os.getcwd()

    # ── HostLifecycle ──────────────────────────────────────────

    @property
    def pid(self) -> int | None:
        return None if self._closed else self._pid

    def read(self, max_bytes: int = 65536, timeout: float | None = 0.2) -> bytes:
        return self._pty.read(max_bytes, timeout)

    def write(self, data: bytes) -> None:
        if not self._closed:
            self._pty.write(data)

    def try_wait(self) -> int | None:
        return self._pty.try_wait()

    def poll_eof(self, stream: Stream = Stream.STDOUT) -> bool:
        return self._pty.poll_eof()

    def kill(self) -> None:
        self._tree.kill()

    def descendants(self) -> tuple[int, ...]:
        return self._tree.descendants()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._tree.close()
        try:
            self._pty.close()
        except Exception as exc:  # 句柄可能已失效；关闭失败不该中断生命周期
            _logger.debug("关闭 PTY 异常 pid=%s: %s", self._pid, exc)

    # ── TerminalHost ───────────────────────────────────────────

    def ingest(self, data: bytes) -> bytes:
        """喂终端模型，返回模型要回写给应用的应答。"""
        return self._screen.feed(data)

    def resize(self, cols: int, rows: int) -> None:
        self._pty.resize(cols, rows)
        self._screen.resize(cols, rows)

    def rebuild_bytes(self) -> bytes:
        return self._screen.rebuild_bytes()

    def screen_text(self) -> str:
        return self._screen.text()

    def full_text(self) -> str:
        return self._screen.full_text()

    def screen_cells(self) -> tuple[tuple[str, ...], ...]:
        return self._screen.cells()

    def render_svg(self) -> str:
        return render_svg(self._screen)

    def render_image(self, *, scale: float = 1.0, fmt: str = "png") -> bytes:
        return render_image(self._screen, scale=scale, fmt=fmt)

    def metadata(self) -> HostMetadata:
        return HostMetadata(title=self._screen.title() or None, cwd=self._fallback_cwd)
