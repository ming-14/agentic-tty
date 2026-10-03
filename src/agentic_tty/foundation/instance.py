"""本机实例互斥：同一个名字上只允许一个进程。

**只管互斥，不管命名**——锁名由 `config.names.lock_name()` 给；这一层不认识"实例"是什么，
只认一个字符串。

锁随进程退出由内核释放（Windows 命名互斥体随进程终止释放，POSIX `flock` 随 fd 关闭释放），
因此**不需要 stale 锁检测**——进程被硬杀也不会留下死锁。

**查询不留副作用**：`is_held()` 不建文件、不删文件——POSIX 上不 `O_CREAT`，Windows 上只
`OpenMutex`（打开，不改动那个对象）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from .logs import get_logger

_logger = get_logger("foundation.instance")

_IS_WINDOWS = sys.platform == "win32"
_ERROR_ALREADY_EXISTS = 183
_SYNCHRONIZE = 0x00100000

_winapi: Any = None


def _k32() -> Any:
    """kernel32 句柄。**只建一次**——原型声明长在实例上，建两个等于声明白费。"""
    global _winapi
    if _winapi is None:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.OpenMutexW.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        _winapi = kernel32
    return _winapi


def _windows_acquire(name: str) -> int | None:
    import ctypes

    kernel32 = _k32()
    handle = kernel32.CreateMutexW(None, False, name)
    if not handle:
        raise OSError(ctypes.get_last_error(), "CreateMutexW 失败")
    if ctypes.get_last_error() == _ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        return None
    return int(handle)


def _windows_release(handle: int) -> None:
    from ctypes import wintypes

    _k32().CloseHandle(wintypes.HANDLE(handle))


def _windows_held(name: str) -> bool:
    """有人开着这个互斥体吗——`OpenMutexW` 只**打开**，不改动它。"""
    kernel32 = _k32()
    handle = kernel32.OpenMutexW(_SYNCHRONIZE, False, name)
    if not handle:
        return False
    kernel32.CloseHandle(handle)
    return True


def _posix_acquire(path: str) -> int | None:
    import fcntl

    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def _posix_release(fd: int) -> None:
    os.close(fd)


def _posix_held(path: str) -> bool:
    """有人拿着这个锁吗——**不 `O_CREAT`**（文件不在就是没人占），拿共享锁试一下就走。

    `LOCK_SH` 与持有者的 `LOCK_EX` 互斥，所以"试得到"就说明没人占；试到立刻放掉。
    """
    import fcntl

    try:
        fd = os.open(path, os.O_RDWR)
    except FileNotFoundError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except OSError:
        return True
    finally:
        os.close(fd)
    return False


def is_held(name: str) -> bool:
    """**有没有进程占着这个锁名**——只读查询，不建文件、不留副作用。

    它回答"这个实例在不在"（**正在初始化也算在**），不回答"能不能连"——那是端点的事：
    连得上就是装好了。
    """
    if _IS_WINDOWS:
        return _windows_held(name)
    return _posix_held(name)


class InstanceLock:
    """跨平台单实例锁。锁名由调用方给（`config.names.lock_name()`）。"""

    def __init__(self, name: str) -> None:
        self._name = name
        self._handle: int | None = None

    @property
    def acquired(self) -> bool:
        return self._handle is not None

    def acquire(self) -> bool:
        """尝试取锁；已被占用返回 False。"""
        if self._handle is not None:
            return True
        if _IS_WINDOWS:
            self._handle = _windows_acquire(self._name)
        else:
            # POSIX 上锁名**就是路径**，所以取锁前得让它的目录在（Windows 上不是路径，跳过）。
            Path(self._name).parent.mkdir(parents=True, exist_ok=True)
            self._handle = _posix_acquire(self._name)
        if self._handle is None:
            _logger.warning("单实例锁已被占用 name=%s", self._name)
            return False
        _logger.info("单实例锁已获取 name=%s", self._name)
        return True

    def release(self) -> None:
        if self._handle is None:
            return
        handle, self._handle = self._handle, None
        if _IS_WINDOWS:
            _windows_release(handle)
        else:
            _posix_release(handle)
        _logger.info("单实例锁已释放 name=%s", self._name)
