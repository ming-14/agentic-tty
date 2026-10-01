"""GUI 窗口探测：判断某个会话是否弹出了窗口。

**平台能力差异（刻意）**：Windows 上 `EnumWindows` + `GetWindowThreadProcessId`
能把窗口归属到 pid，于是"这个会话弹了窗口"是可判定的；Linux 上不行——X11 勉强
能靠 `_NET_WM_PID` 拼凑，但 **Wayland 根本不允许客户端查询其他进程的窗口**。
因此 Linux 分支返回空元组（首次调用记一条告警），调用方得到的是"查不到窗口"。
"""

from __future__ import annotations

import sys
from collections.abc import Iterable
from dataclasses import dataclass

from ...foundation.logs import get_logger

_logger = get_logger("runtime.monitor.windows")

_IS_WINDOWS = sys.platform == "win32"
_GW_OWNER = 4

_warned_unsupported = False


@dataclass(frozen=True, slots=True)
class WindowInfo:
    """一个可见的顶层窗口。"""

    pid: int
    handle: int
    title: str


def windows_of(pids: Iterable[int]) -> tuple[WindowInfo, ...]:
    """列出属于 `pids` 的**可见顶层窗口**。

    只算可见且没有属主的顶层窗口：子控件、工具窗、隐藏窗口都不算"弹出了 GUI"。
    控制台程序的窗口属于 conhost 而非程序本身，因此不会被误判。
    """
    global _warned_unsupported
    if not _IS_WINDOWS:
        if not _warned_unsupported:
            _warned_unsupported = True
            _logger.warning("窗口探测在 Linux 上不可用，恒返回空结果")
        return ()
    wanted = frozenset(pids)
    if not wanted:
        return ()
    return _windows_windows_of(wanted)


def _windows_windows_of(pids: frozenset[int]) -> tuple[WindowInfo, ...]:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [enum_proc, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetWindow.restype = wintypes.HWND
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int

    found: list[WindowInfo] = []

    @enum_proc
    def _visit(hwnd: int, _lparam: int) -> bool:
        pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value not in pids:
            return True
        if not user32.IsWindowVisible(hwnd) or user32.GetWindow(hwnd, _GW_OWNER):
            return True
        # 取别的进程的标题读的是窗口缓存，不会发跨进程消息，因此不会因对方卡住而阻塞
        length = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        found.append(WindowInfo(pid=pid.value, handle=int(hwnd), title=buf.value))
        return True

    user32.EnumWindows(_visit, 0)
    return tuple(found)
