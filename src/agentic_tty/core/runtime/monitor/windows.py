"""Windows 窗口探测：`EnumWindows` + `GetWindowThreadProcessId` 把窗口归属到 pid。"""

from __future__ import annotations

from .probe import WindowInfo

_GW_OWNER = 4


def probe_windows(pids: frozenset[int]) -> tuple[WindowInfo, ...]:
    """列出属于 `pids` 的可见顶层窗口。"""
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
