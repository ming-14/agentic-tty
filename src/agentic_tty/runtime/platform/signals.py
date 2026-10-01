"""信号处理：把终止信号转成一个回调。

`signal.signal` 只能在主线程注册，非主线程调用会抛 `ValueError`——
调用方需自行确保在主线程安装。
"""

from __future__ import annotations

import signal
from collections.abc import Callable

from ...foundation.logs import get_logger

_logger = get_logger("runtime.signals")


def install_shutdown_handler(callback: Callable[[int], None]) -> list[int]:
    """安装 SIGINT / SIGTERM 处理器，返回实际安装成功的信号列表。"""
    installed: list[int] = []
    for name in ("SIGINT", "SIGTERM"):
        signum = getattr(signal, name, None)
        if signum is None:
            continue
        try:
            signal.signal(signum, lambda s, _frame: callback(s))
        except (ValueError, OSError) as exc:  # 非主线程等
            _logger.warning("安装 %s 处理器失败: %s", name, exc)
            continue
        installed.append(int(signum))
    return installed
