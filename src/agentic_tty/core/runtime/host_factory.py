"""宿主工厂与启动期依赖检查。"""

from __future__ import annotations

from ..ports import LOCALPTY, PTY, SUBPROCESS, HostLifecycle, SessionSpec
from .errors import DependencyMissing, HostSpawnError
from .pty_host import PtyHost, require_pywezterm
from .subprocess_host import SubprocessHost


def create_host(spec: SessionSpec) -> HostLifecycle:
    """按模式标签选择宿主（内置 pty / subprocess / localpty 三种）。"""
    if spec.mode == PTY:
        return PtyHost(spec)
    if spec.mode == LOCALPTY:
        # 惰性导入：纯 pty / subprocess 场景不该把 pyte 与位图渲染器拖进来
        from .localpty import LocalPtyHost

        return LocalPtyHost(spec)
    if spec.mode == SUBPROCESS:
        return SubprocessHost(spec)
    raise HostSpawnError(f"未知会话模式: {spec.mode!r}")


def check_dependencies() -> None:
    """启动期依赖检查。

    不可用就**拒绝启动**（由调用方决定退出码），绝不静默降级成一个
    "起来了却建不出会话"的进程。
    """
    try:
        require_pywezterm()
    except DependencyMissing as exc:
        raise DependencyMissing(f"启动依赖检查失败: {exc}") from exc


__all__ = ["check_dependencies", "create_host"]
