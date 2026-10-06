"""子进程原语 → 一台**已经连上子进程**的双管道会话。

与 `local_pty/console.py` 同一个位置：宿主（`host.py`）只管生命周期与端口语义，
"子进程怎么起、它的 IO 从哪来"在这里抽成 `ProcessLauncher` 接缝。自带那份用
`subprocess.Popen`；沙箱换掉同一个可调用对象即可把受限 spawn 接进来（句柄包成 fd）。

读放这里而不是宿主里：`Popen` 的管道与沙箱的句柄都是"裸 fd + 超时等待"，
同一份 `read_fd` 两边共用。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from ....foundation.logs import get_logger
from ...ports import SessionSpec
from ..errors import HostSpawnError
from ..process_tree import (
    CREATE_SUSPENDED,
    ProcessTree,
    Tree,
    assign_job,
    close_job,
    create_job,
    resume_process,
)

_logger = get_logger("core.runtime.subprocess.pipes")

_IS_WINDOWS = sys.platform == "win32"

_PEEK_INTERVAL = 0.005
"""`PeekNamedPipe` 的轮询间隔——与读线程空读时的停顿同量级（见 `reader.py`）。"""


def _wait_readable(fd: int, timeout: float) -> bool:
    """等 fd 可读或超时。"""
    if _IS_WINDOWS:
        return _windows_wait_readable(fd, timeout)
    import selectors

    with selectors.DefaultSelector() as selector:
        selector.register(fd, selectors.EVENT_READ)
        return bool(selector.select(timeout))


def _windows_wait_readable(fd: int, timeout: float) -> bool:  # pragma: no cover - 平台分支
    """等 fd 有数据、或写端已关、或超时。

    **不能用 `WaitForSingleObject`**：匿名管道的句柄没有"读就绪"语义——实测无数据时
    它也立刻返回 signaled，拿它当判据等于不等，`os.read` 直接阻塞到有数据或 EOF
    （超时读静默退化成阻塞读）。问 `PeekNamedPipe`"现在有多少字节"才是准的。
    """
    import ctypes
    import msvcrt
    from ctypes import wintypes

    handle = wintypes.HANDLE(msvcrt.get_osfhandle(fd))
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.PeekNamedPipe.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.c_void_p,
    ]
    kernel32.PeekNamedPipe.restype = wintypes.BOOL

    deadline = time.monotonic() + timeout
    while True:
        available = wintypes.DWORD(0)
        if not kernel32.PeekNamedPipe(handle, None, 0, None, ctypes.byref(available), None):
            return True  # 管道断了：让上层去读，拿到 EOF 的空返回
        if available.value:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(_PEEK_INTERVAL)


def read_fd(fd: int, max_bytes: int, timeout: float | None) -> bytes:
    """从管道 fd 读一段。

    空返回值是"本轮无数据（超时）"或 EOF，两者由调用方拿 `try_wait()` 区分——
    与 `HostLifecycle.read` 的口径一致。
    """
    if timeout is None:
        return os.read(fd, max_bytes)  # 阻塞；EOF 返回 b""
    if not _wait_readable(fd, timeout):
        return b""
    return os.read(fd, max_bytes)


class ProcessPrimitive(Protocol):
    """双管道原语的 IO 面：读两路输出、写 stdin、查退出码、关闭。"""

    def read(self, max_bytes: int = 65536, timeout: float | None = 0.2) -> bytes:
        """读主输出流（stdout）一段。**只允许读线程调用。**"""
        ...

    def read_stderr(self, max_bytes: int = 65536, timeout: float | None = 0.2) -> bytes:
        """读 stderr 一段。**只允许读线程调用。**"""
        ...

    def write(self, data: bytes) -> None:
        """写 stdin。**唯一调用者必须是写线程**（缓冲写满时会阻塞）。"""
        ...

    def close_stdin(self) -> None:
        """关掉 stdin 发 EOF（`cat`、`python -` 这类程序在等它）。"""
        ...

    def try_wait(self) -> int | None:
        """非阻塞查退出码；None = 仍在运行。"""
        ...

    def close(self) -> None:
        """释放 IO 资源（幂等）。"""
        ...


@dataclass(frozen=True, slots=True)
class ProcessConsole:
    """一台连好子进程的双管道会话：IO 面 + 子进程 pid + 整棵树的观测与终止。"""

    pipes: ProcessPrimitive
    pid: int
    tree: Tree


ProcessLauncher = Callable[[SessionSpec], ProcessConsole]
"""起一台连好子进程的双管道会话。换掉它 = 换掉"子进程怎么起"（沙箱就是这么接进来的）。"""


class PopenPipes:
    """`subprocess.Popen` 的三个管道（自带路径用）。"""

    def __init__(self, proc: subprocess.Popen) -> None:
        self._proc = proc
        self._closed = False

    def read(self, max_bytes: int = 65536, timeout: float | None = 0.2) -> bytes:
        if self._closed or self._proc.stdout is None:
            return b""
        return read_fd(self._proc.stdout.fileno(), max_bytes, timeout)

    def read_stderr(self, max_bytes: int = 65536, timeout: float | None = 0.2) -> bytes:
        if self._closed or self._proc.stderr is None:
            return b""
        return read_fd(self._proc.stderr.fileno(), max_bytes, timeout)

    def write(self, data: bytes) -> None:
        stdin = self._proc.stdin
        if stdin is None or self._closed:
            return
        try:
            stdin.write(data)
            stdin.flush()
        except (BrokenPipeError, ValueError):
            pass  # 子进程已关 stdin：不是错误

    def close_stdin(self) -> None:
        stdin = self._proc.stdin
        if stdin is None:
            return
        try:
            stdin.close()
        except OSError:
            pass

    def try_wait(self) -> int | None:
        if self._closed:
            return None
        return self._proc.poll()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for pipe in (self._proc.stdin, self._proc.stdout, self._proc.stderr):
            if pipe is not None:
                try:
                    pipe.close()
                except OSError:
                    pass


def open_process(spec: SessionSpec) -> ProcessConsole:
    """自带路径：Popen 双管道。

    Windows 上先建作业、再以挂起态创建：进程在入作业前不执行任何指令，因此不可能
    已经 fork 出逃逸的孙进程。`start_new_session`（POSIX）/ `CREATE_NEW_PROCESS_GROUP`
    （Windows）让它自成进程组，便于整棵树强杀。
    """
    env = dict(os.environ)
    env.update(spec.env)
    job = create_job()
    extra: dict = {}
    if _IS_WINDOWS:
        flags = subprocess.CREATE_NEW_PROCESS_GROUP
        if job is not None:
            flags |= CREATE_SUSPENDED
        extra["creationflags"] = flags
    else:
        extra["start_new_session"] = True
    try:
        proc = subprocess.Popen(
            list(spec.argv),
            cwd=spec.cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **extra,
        )
    except OSError as exc:
        close_job(job)
        raise HostSpawnError(f"启动子进程失败 {list(spec.argv)}: {exc}") from exc
    if job is not None:
        try:
            if not assign_job(job, proc.pid):
                # 入作业失败就必须丢掉作业身份：留着它会让 kill 走作业分支（空作业 = 没杀）、
                # descendants 误报空列表。退回按 pid 终止，观测由 MonitorUnavailable 报错。
                _logger.warning("子进程加入作业对象失败，改用按 pid 终止 pid=%s", proc.pid)
                close_job(job)
                job = None
            # 入作业成败都必须恢复，否则挂起的子进程永远不会运行
            resume_process(proc.pid)
        except Exception:
            proc.kill()  # 恢复失败会让子进程永远挂在挂起态，先收掉
            close_job(job)
            raise
    return ProcessConsole(pipes=PopenPipes(proc), pid=proc.pid, tree=ProcessTree(proc.pid, job))
