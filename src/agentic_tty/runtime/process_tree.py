"""进程树追踪与强杀。

守护进程退出时，会话必须一起收尾，否则会留下占着 PTY 的孤儿进程。
正常退出走优雅收尾，**异常崩溃由内核兜底**：

- Windows：Job Object 设 `KILL_ON_JOB_CLOSE`——作业句柄一关（进程死），
  作业内所有进程被内核杀掉。
- Linux：子进程自成进程组（`start_new_session`），强杀时按进程组发信号。

已知边界：Windows 上"创建后加入作业"存在极小的竞态窗口（子进程若在加入前
就 fork 出孙进程，那些孙进程会逃出作业）。要彻底消除得让子进程以挂起态创建、
加入作业后再恢复，宿主创建路径目前不暴露该能力。
"""

from __future__ import annotations

import os
import signal
import sys

from ..foundation.logs import get_logger

_logger = get_logger("runtime.process_tree")

_IS_WINDOWS = sys.platform == "win32"
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_JobObjectExtendedLimitInformation = 9
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_TERMINATE = 0x0001


if _IS_WINDOWS:  # pragma: no cover - 平台分支
    import ctypes
    from ctypes import wintypes

    class _IoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class _BasicLimit(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _ExtendedLimit(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BasicLimit),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]


def _windows_open_job(pid: int) -> int | None:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.OpenProcess.restype = wintypes.HANDLE

    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        return None
    info = _ExtendedLimit()
    info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel32.SetInformationJobObject(
        job, _JobObjectExtendedLimitInformation, ctypes.byref(info), ctypes.sizeof(info)
    ):
        kernel32.CloseHandle(job)
        return None
    proc = kernel32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid)
    if not proc:
        kernel32.CloseHandle(job)
        return None
    assigned = kernel32.AssignProcessToJobObject(job, proc)
    kernel32.CloseHandle(proc)
    if not assigned:
        kernel32.CloseHandle(job)
        return None
    return int(job)


def _windows_terminate_job(job: int) -> None:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateJobObject(wintypes.HANDLE(job), 1)


def _windows_close_handle(handle: int) -> None:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CloseHandle(wintypes.HANDLE(handle))


class ProcessTree:
    """一棵会话进程树：强杀与兜底清理。"""

    def __init__(self, pid: int) -> None:
        self._pid = pid
        self._job: int | None = None
        if _IS_WINDOWS and pid > 0:
            self._job = _windows_open_job(pid)
            if self._job is None:
                _logger.warning("加入作业对象失败，进程树兜底降级为按 pid 终止 pid=%s", pid)

    @property
    def pid(self) -> int:
        return self._pid

    def kill(self) -> None:
        """强杀整棵树。"""
        if self._job is not None:
            _windows_terminate_job(self._job)
            return
        if _IS_WINDOWS:
            if self._pid > 0:
                os.kill(self._pid, signal.SIGTERM)
            return
        try:
            os.killpg(os.getpgid(self._pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                os.kill(self._pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass

    def close(self) -> None:
        """释放追踪资源；Windows 上句柄一关，作业内残余进程被内核收掉。"""
        if self._job is not None:
            _windows_close_handle(self._job)
            self._job = None
