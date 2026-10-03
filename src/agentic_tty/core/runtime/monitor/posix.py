"""POSIX 窗口探测：**没有实现**，恒返回空。

Windows 上 `EnumWindows` 能把窗口归属到 pid；POSIX 平台（Linux 等）上不行——
X11 勉强能靠 `_NET_WM_PID` 拼，但 **Wayland 根本不允许客户端查询其他进程的窗口**
（安全模型如此）。
所以这里只记一条一次性告警并返回空，调用方得到的是"查不到窗口"。

要支持 X11 会话时在此扩展（需要额外依赖或外部命令）。
"""

from __future__ import annotations

from ....foundation.logs import get_logger
from .probe import WindowInfo

_logger = get_logger("core.runtime.monitor.posix")

_warned = False


def probe_windows(pids: frozenset[int]) -> tuple[WindowInfo, ...]:
    global _warned
    if not _warned:
        _warned = True
        _logger.warning("窗口探测在 POSIX 平台上不可用，恒返回空结果")
    return ()
