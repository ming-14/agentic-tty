"""宿主实现错误。"""

from ...foundation.errors import AgenticTtyError


class RuntimeFailure(AgenticTtyError):
    """宿主实现错误基类。"""


class DependencyMissing(RuntimeFailure):
    """长期依赖（原生扩展等）不可用——依赖它的那个模式建不出会话，不静默降级。"""


class HostSpawnError(RuntimeFailure):
    """宿主进程创建失败。"""


class MonitorUnavailable(RuntimeFailure):
    """**本次**观测失败（本该能观测却观测不到）——显式报错，不静默返回空结果。

    与平台能力缺失分开：平台缺失（如 Linux 上的窗口探测）走"返回空 + 一次性告警"，
    因为那是已知且刻意的差异；这里是会话级的失败，静默返回空会让上层把
    "确实没有子进程"和"看不到子进程"混为一谈。
    """
