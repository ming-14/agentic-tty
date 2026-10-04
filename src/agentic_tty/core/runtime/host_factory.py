"""宿主工厂：按模式标签选宿主。

依赖是**各宿主自己的**（`pty` 要 pywezterm、`localpty` 要平台原语、`subprocess` 无），
在宿主构造时惰性导入、缺了抛 `DependencyMissing`。所以这里不做进程级依赖检查——那等于
把某一个模式的前置条件当成整个进程的前置条件。
"""

from __future__ import annotations

from ..ports import LOCALPTY, PTY, SUBPROCESS, HostLifecycle, SessionSpec
from .errors import HostSpawnError
from .pywezterm_pty import PtyHost
from .subprocess import SubprocessHost


def create_host(spec: SessionSpec) -> HostLifecycle:
    """按模式标签选择宿主（内置 pty / subprocess / localpty 三种）。"""
    if spec.mode == PTY:
        return PtyHost(spec)
    if spec.mode == LOCALPTY:
        # 惰性导入：纯 pty / subprocess 场景不该把 pyte 与位图渲染器拖进来
        from .local_pty import LocalPtyHost

        return LocalPtyHost(spec)
    if spec.mode == SUBPROCESS:
        return SubprocessHost(spec)
    raise HostSpawnError(f"未知会话模式: {spec.mode!r}")


__all__ = ["create_host"]
