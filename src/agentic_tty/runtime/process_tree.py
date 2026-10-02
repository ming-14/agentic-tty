"""进程树追踪与强杀。

守护进程退出时，会话必须一起收尾，否则会留下占着 PTY 的孤儿进程。
正常退出走优雅收尾，**异常崩溃由内核兜底**：

- Windows：Job Object 设 `KILL_ON_JOB_CLOSE`——作业句柄一关（进程死），
  作业内所有进程被内核杀掉。
- Linux：子进程自成进程组（`start_new_session`），强杀时按进程组发信号。

**成员枚举**（`descendants()`）供上层观测"谁起来了、谁没了"，是**轮询式**的：
调用方比对前后两次结果即可得出启动 / 终止事件，代价是**可能漏掉两次轮询之间
"起又没"的短命进程**——事件式观测在 Linux 上不可得（netlink `PROC_EVENTS` 需
`CAP_NET_ADMIN`），而这条返回条件的跨平台语义必须一致，故只提供轮询式。

作业对象在 spawn **之前**建好（`create_job()`），句柄交给宿主在创建子进程时直接
入作业（PTY 走 `PROC_THREAD_ATTRIBUTE_JOB_LIST`，子进程走 `CREATE_SUSPENDED` →
入作业 → `resume_process()`），因此**不存在"创建后再赋值"的时间窗**——窗口期内
fork 出的孙进程会逃出作业，从此既枚举不到也杀不到。
"""

from __future__ import annotations

import os
import signal
import sys

from ..foundation.logs import get_logger
from .errors import MonitorUnavailable

_logger = get_logger("runtime.process_tree")

_IS_WINDOWS = sys.platform == "win32"
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_JobObjectExtendedLimitInformation = 9
_JobObjectBasicProcessIdList = 3
_ERROR_MORE_DATA = 234
_MAX_JOB_PIDS = 1 << 20
# 首次查询的槽位数；不够就倍增。抽成常量是为了让"被截断→扩容"这条路径可测
_JOB_PIDS_INITIAL_CAPACITY = 256
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_TERMINATE = 0x0001
_TH32CS_SNAPTHREAD = 0x00000004
_THREAD_SUSPEND_RESUME = 0x0002

# subprocess 模块只导出了 CREATE_NEW_PROCESS_GROUP 等少数几个创建标志，
# 没有 CREATE_SUSPENDED，这里自己定义
CREATE_SUSPENDED = 0x00000004


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


def _windows_create_job() -> int | None:
    """建一个带 `KILL_ON_JOB_CLOSE` 的作业对象；失败返回 None。"""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL

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
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.CloseHandle(wintypes.HANDLE(handle))


def _windows_job_pids(job: int) -> tuple[int, ...]:
    """查作业对象当前的进程列表（`JobObjectBasicProcessIdList`）。

    比全局快照准：拿到的就是这个会话的进程，没有 PID 复用与孤儿父进程的问题。

    `ProcessIdList` 是变长数组，所需大小要先查一次才知道，因此按容量倍增重查。
    **实测**：本信息类在缓冲不足时既不报错也不给所需大小，而是**静默截断**
    （返回成功、`NumberOfProcessIdsInList == capacity`），所以主要靠"列表被填满"
    判定扩容；同时兜住文档所写的 `ERROR_MORE_DATA` 语义，两种行为都能收敛。
    """
    import ctypes
    from ctypes import wintypes

    class _Header(ctypes.Structure):
        _fields_ = [
            ("NumberOfAssignedProcesses", wintypes.DWORD),
            ("NumberOfProcessIdsInList", wintypes.DWORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.QueryInformationJobObject.restype = wintypes.BOOL

    header_size = ctypes.sizeof(_Header)
    step = ctypes.sizeof(ctypes.c_void_p)
    capacity = _JOB_PIDS_INITIAL_CAPACITY
    while True:
        buf = ctypes.create_string_buffer(header_size + step * capacity)
        returned = wintypes.DWORD(0)
        ok = kernel32.QueryInformationJobObject(
            wintypes.HANDLE(job),
            _JobObjectBasicProcessIdList,
            buf,
            len(buf),
            ctypes.byref(returned),
        )
        if not ok:
            code = ctypes.get_last_error()
            if code != _ERROR_MORE_DATA:
                raise MonitorUnavailable(f"查询作业对象进程列表失败（错误码 {code}）")
        else:
            count = _Header.from_buffer(buf).NumberOfProcessIdsInList
            if count < capacity:  # 列表装得下：完整
                base = ctypes.addressof(buf) + header_size
                return tuple(
                    ctypes.c_void_p.from_address(base + i * step).value or 0 for i in range(count)
                )
            # count == capacity：被截断（实测本信息类不报错，只是静默截断）
        if capacity > _MAX_JOB_PIDS:
            raise MonitorUnavailable("作业对象进程列表异常庞大，放弃枚举")
        capacity *= 2


def _linux_descendants(root: int) -> tuple[int, ...]:
    """递归读 `/proc/<pid>/task/<tid>/children` 收集整棵树。

    只走树上的节点，不像全局 `/proc` 扫描那样随机器上的进程数线性变慢。
    """
    found: set[int] = set()
    pending = [root]
    while pending:
        pid = pending.pop()
        try:
            tids = os.listdir(f"/proc/{pid}/task")
        except OSError:  # 进程已退出：树在这里断掉
            continue
        for tid in tids:
            try:
                with open(f"/proc/{pid}/task/{tid}/children", "rb") as fh:
                    children = [int(token) for token in fh.read().split()]
            except (OSError, ValueError):
                continue
            for child in children:
                if child not in found:
                    found.add(child)
                    pending.append(child)
    return tuple(sorted(found))


def _terminate_pid(pid: int) -> None:
    """按 pid 终止单个进程（Windows 没有作业对象时的退路）。

    进程若已退出，`TerminateProcess` 会以 `ERROR_ACCESS_DENIED` 失败——"已经死了"
    不是错误，照 Linux 分支一样不往上抛。真杀不动（如子进程提权）时这里只留一条
    日志，由上层等退出码超时去暴露。
    """
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as exc:
        _logger.debug("按 pid 终止失败 pid=%s: %s", pid, exc)


def create_job() -> int | None:
    """建一个作业对象；非 Windows 返回 None。

    必须在 spawn **之前**建好，句柄交给宿主在创建子进程时直接入作业。创建后再
    `AssignProcessToJobObject` 存在时间窗：子进程在此期间 fork 出的孙进程不会进
    作业，从此既枚举不到也杀不到。
    """
    return _windows_create_job() if _IS_WINDOWS else None


def close_job(job: int | None) -> None:
    """关闭作业句柄；Windows 上句柄一关，作业内残余进程由内核收掉。"""
    if job is not None:
        _windows_close_handle(job)


def assign_job(job: int, pid: int) -> bool:
    """把进程放进作业，成功返回 True。

    只有子进程**以挂起态创建**（`CREATE_SUSPENDED`）时用它才无竞态——进程还没执行
    任何指令，不可能已经 fork 出孙进程。
    """
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL

    proc = kernel32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid)
    if not proc:
        return False
    try:
        return bool(kernel32.AssignProcessToJobObject(wintypes.HANDLE(job), proc))
    finally:
        kernel32.CloseHandle(proc)


def resume_process(pid: int) -> None:
    """恢复以挂起态创建的进程；非 Windows 无操作。

    挂起创建是"先入作业、再放行"的前提：放行靠枚举该进程的线程逐个 `ResumeThread`。
    """
    if _IS_WINDOWS:
        _windows_resume_process(pid)


def _windows_resume_process(pid: int) -> None:
    import ctypes
    from ctypes import wintypes

    class _ThreadEntry32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ThreadID", wintypes.DWORD),
            ("th32OwnerProcessID", wintypes.DWORD),
            ("tpBasePri", wintypes.LONG),
            ("tpDeltaPri", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ThreadEntry32)]
    kernel32.Thread32First.restype = wintypes.BOOL
    kernel32.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ThreadEntry32)]
    kernel32.Thread32Next.restype = wintypes.BOOL
    kernel32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenThread.restype = wintypes.HANDLE
    kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
    kernel32.ResumeThread.restype = wintypes.DWORD

    snap = kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPTHREAD, 0)
    if snap == ctypes.c_void_p(-1).value:
        raise MonitorUnavailable("CreateToolhelp32Snapshot 失败，挂起的进程无法恢复")
    try:
        entry = _ThreadEntry32()
        entry.dwSize = ctypes.sizeof(_ThreadEntry32)
        more = kernel32.Thread32First(snap, ctypes.byref(entry))
        while more:
            if entry.th32OwnerProcessID == pid:
                thread = kernel32.OpenThread(_THREAD_SUSPEND_RESUME, False, entry.th32ThreadID)
                if thread:
                    kernel32.ResumeThread(thread)
                    kernel32.CloseHandle(thread)
            more = kernel32.Thread32Next(snap, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snap)


class ProcessTree:
    """一棵会话进程树：强杀、兜底清理与成员枚举。"""

    def __init__(self, pid: int, job: int | None = None) -> None:
        self._pid = pid
        self._job = job
        if _IS_WINDOWS and pid > 0 and job is None:
            _logger.warning("会话进程不在作业对象里，只能按 pid 终止、无法枚举成员 pid=%s", pid)

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
                _terminate_pid(self._pid)
            return
        try:
            os.killpg(os.getpgid(self._pid), signal.SIGKILL)
        except OSError:  # 进程组没了（含 ProcessLookupError）或没权限：退到杀单进程
            try:
                os.kill(self._pid, signal.SIGKILL)
            except OSError:
                pass

    def close(self) -> None:
        """释放追踪资源；Windows 上句柄一关，作业内残余进程被内核收掉。"""
        if self._job is not None:
            _windows_close_handle(self._job)
            self._job = None

    def descendants(self) -> tuple[int, ...]:
        """当前进程树里**除根进程外**的成员 pid（升序）。

        轮询式观测：调用方比对前后两次结果，即可得出"谁起来了、谁没了"。
        观测不到时**显式报错**，不返回空元组——空元组是"确实没有子进程"，
        两者混同会让"查到子进程启动→终止"这类条件静默失效。
        """
        if self._pid <= 0:
            raise MonitorUnavailable("进程树没有根进程 pid，无法枚举成员")
        if _IS_WINDOWS:
            if self._job is None:
                raise MonitorUnavailable("进程树不在作业对象里，无法枚举成员")
            members = _windows_job_pids(self._job)
        else:
            members = _linux_descendants(self._pid)
        return tuple(pid for pid in members if pid != self._pid)
