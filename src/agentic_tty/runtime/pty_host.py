"""pywezterm 宿主：PTY + 终端模型。

`pywezterm` 不在 PyPI 上、也不安装：它是仓库 `vendor/` 里的长期依赖，由
`runtime/vendor.py` 接进 `sys.path`。因此这里做**惰性导入**——真正需要 PTY
时才导入扩展，纯子进程场景不碰它。
"""

from __future__ import annotations

import os
from types import ModuleType

from ..core.ports import HostMetadata, SessionSpec
from ..foundation.logs import get_logger
from .errors import DependencyMissing, HostSpawnError
from .process_tree import ProcessTree

_logger = get_logger("runtime.pty_host")

_pywezterm: ModuleType | None = None


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


class PtyHost:
    """伪终端宿主。"""

    def __init__(self, spec: SessionSpec) -> None:
        pw = require_pywezterm()
        self._pty = pw.Pty(cols=spec.cols, rows=spec.rows)
        self._term = pw.Terminal(cols=spec.cols, rows=spec.rows)
        env = dict(os.environ)
        env.update(spec.env)
        try:
            self._pty.spawn(list(spec.argv), cwd=spec.cwd, env=env)
        except Exception as exc:
            raise HostSpawnError(f"启动 PTY 失败 {list(spec.argv)}: {exc}") from exc
        self._tree = ProcessTree(self._pty.child_pid() or 0)
        self._closed = False

    # ── HostLifecycle ──────────────────────────────────────────

    @property
    def pid(self) -> int | None:
        return None if self._closed else self._pty.child_pid()

    def read(self, max_bytes: int = 65536, timeout: float | None = 0.2) -> bytes:
        return self._pty.read(max_bytes, timeout=timeout)

    def write(self, data: bytes) -> None:
        if not self._closed:
            self._pty.write(data)

    def try_wait(self) -> int | None:
        return self._pty.try_wait()

    def kill(self) -> None:
        self._tree.kill()

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

    def snapshot(self) -> bytes:
        """重建字节：RIS + 模式恢复 + scrollback + 可见区。"""
        parts = [b"\x1bc"]
        parts.append(_to_bytes(self._term.mode_restore_seq()))
        parts.append(_to_bytes(self._term.render_scrollback(keep_ansi=True)))
        parts.append(_to_bytes(self._term.render_ansi(include_cursor=True)))
        return b"".join(parts)

    def metadata(self) -> HostMetadata:
        return HostMetadata(
            title=self._term.get_title(),
            cwd=self._term.get_current_dir(),
        )

    def render_svg(self) -> str:
        """可见屏幕的 SVG。底层该参数必填，固定 0 = 不压缩。"""
        return self._term.render_svg(0)

    def render_image(self, *, scale: float = 1.0, fmt: str = "png") -> bytes:
        """可见屏幕的位图。"""
        return self._term.render_image(scale=scale, fmt=fmt)

    # ── 观测 ───────────────────────────────────────────────────

    def screen_text(self) -> str:
        """可见屏幕纯文本（管理台显示用）。"""
        return self._term.text()

    def render_ansi(self) -> str:
        return self._term.render_ansi(include_cursor=True)
