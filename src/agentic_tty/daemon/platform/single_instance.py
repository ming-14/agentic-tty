"""单实例互斥。

锁随进程退出由内核释放（Windows 命名互斥体随进程终止释放，POSIX `flock`
随 fd 关闭释放），因此**不需要额外的 stale 锁检测**。

会话坐标（offset、日志、订阅表）活在单进程内存里，两个守护进程会各自维护
互斥的 offset；接入点名字也会冲突。所以这是硬要求，不提供关闭开关。
"""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path
from typing import Any

from ...foundation.logs import get_logger

_logger = get_logger("daemon.platform.single_instance")

_IS_WINDOWS = sys.platform == "win32"
_ERROR_ALREADY_EXISTS = 183

_winapi: Any = None


def _mutex_name(name: str, runtime_dir: Path) -> str:
    """Windows 命名互斥体名：把 `runtime_dir` 也并进命名空间。

    POSIX 的锁文件天然落在 `runtime_dir` 里，Windows 的互斥体名却是全局的——
    不并目录的话，"同名不同目录"的两份配置会互撞，与 POSIX 行为不一致。
    """
    digest = hashlib.sha256(str(runtime_dir).encode("utf-8")).hexdigest()[:16]
    return f"Local\\{name}-{digest}"


def _k32() -> Any:
    """kernel32 句柄。**只建一次**——原型声明长在实例上，建两个等于声明白费。"""
    global _winapi
    if _winapi is None:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.CreateMutexW.restype = wintypes.HANDLE
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


def _posix_acquire(path: Path) -> int | None:
    import fcntl

    fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def _posix_release(fd: int) -> None:
    os.close(fd)


class SingleInstance:
    """跨平台单实例锁。"""

    def __init__(self, name: str, runtime_dir: Path) -> None:
        self._name = name
        self._runtime_dir = runtime_dir
        self._handle: int | None = None

    @property
    def acquired(self) -> bool:
        return self._handle is not None

    def acquire(self) -> bool:
        """尝试取锁；已被占用返回 False。"""
        if self._handle is not None:
            return True
        self._runtime_dir.mkdir(parents=True, exist_ok=True)
        if _IS_WINDOWS:
            self._handle = _windows_acquire(_mutex_name(self._name, self._runtime_dir))
        else:
            self._handle = _posix_acquire(self._runtime_dir / f"{self._name}.lock")
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
