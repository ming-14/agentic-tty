"""GUI 窗口探测：判断某个会话是否弹出了窗口。

按平台分派到 `windows.py` / `posix.py`；`WindowInfo` 在这里定义，两边共用。
分派用**函数内局部 import**——加载本模块时不碰平台实现，也就不会与它们互相 import。
"""

from __future__ import annotations

import sys
from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class WindowInfo:
    """一个可见的顶层窗口。"""

    pid: int
    handle: int
    title: str


def windows_of(pids: Iterable[int]) -> tuple[WindowInfo, ...] | None:
    """列出属于 `pids` 的**可见顶层窗口**；平台不支持探测时返回 `None`。

    只算可见且没有属主的顶层窗口：子控件、工具窗、隐藏窗口都不算"弹出了 GUI"。
    控制台程序的窗口属于 conhost 而非程序本身，因此不会被误判。

    **`None` 与空元组是两回事**：空元组是"确实没有窗口"，`None` 是"这个平台查不到"
    （POSIX 上窗口探测没有实现，见 `posix.py`）。混同会让调用方把"看不到"当成"没有"。
    """
    wanted = frozenset(pids)
    if not wanted:
        return ()
    if sys.platform == "win32":
        from .windows import probe_windows

        return probe_windows(wanted)
    from .posix import probe_windows

    return probe_windows(wanted)
