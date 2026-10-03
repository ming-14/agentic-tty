"""信号处理：把终止信号转成一个回调。

`signal.signal` 只能在主线程注册，非主线程调用会抛 `ValueError`——
调用方需自行确保在主线程安装。

信号处理器是**进程级**的：库被进程内嵌（测试、验证台）时，装完不还原就会泄漏到
调用方进程里，所以这里把"原值"一并带回来，停止时按原值还回去。
"""

from __future__ import annotations

import signal
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ...foundation.logs import get_logger

_logger = get_logger("daemon.platform.signals")

_HANDLED = ("SIGINT", "SIGTERM")


@dataclass(frozen=True, slots=True)
class InstalledSignals:
    """已装上的处理器与它们的原值。"""

    handlers: tuple[tuple[int, Any], ...] = ()

    @property
    def numbers(self) -> tuple[int, ...]:
        return tuple(number for number, _ in self.handlers)

    def restore(self) -> None:
        """把处理器还原成装上之前的样子。"""
        for number, previous in self.handlers:
            try:
                signal.signal(number, previous if previous is not None else signal.SIG_DFL)
            except (ValueError, OSError) as exc:  # 非主线程等
                _logger.warning("还原信号 %s 失败: %s", number, exc)


def install_shutdown_handler(callback: Callable[[int], None]) -> InstalledSignals:
    """安装 SIGINT / SIGTERM 处理器，返回可还原的句柄。"""
    handlers: list[tuple[int, Any]] = []
    for name in _HANDLED:
        signum = getattr(signal, name, None)
        if signum is None:
            continue
        try:
            previous = signal.signal(signum, lambda s, _frame: callback(s))
        except (ValueError, OSError) as exc:  # 非主线程等
            _logger.warning("安装 %s 处理器失败: %s", name, exc)
            continue
        handlers.append((int(signum), previous))
    return InstalledSignals(tuple(handlers))
