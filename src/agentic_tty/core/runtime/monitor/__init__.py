"""监控：会话之外的可观测状态。

只放**不依赖宿主句柄**的观测；需要宿主句柄的（进程树成员）跟着 `ProcessTree` 走，
免得把作业对象的代码拆到两个文件里。
"""

from .probe import WindowInfo, windows_of

__all__ = ["WindowInfo", "windows_of"]
