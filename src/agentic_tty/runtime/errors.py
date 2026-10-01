"""运行时层错误。"""

from __future__ import annotations

from ..foundation.errors import AgenticTtyError


class RuntimeFailure(AgenticTtyError):
    """运行时层错误基类。"""


class DependencyMissing(RuntimeFailure):
    """长期依赖（原生扩展等）不可用——拒绝启动，不静默降级。"""


class InstanceLocked(RuntimeFailure):
    """单实例锁已被占用。"""


class HostSpawnError(RuntimeFailure):
    """宿主进程创建失败。"""
