"""子进程宿主：`Popen` 双管道，无终端模型。

`start_new_session`（POSIX）/ `CREATE_NEW_PROCESS_GROUP`（Windows）让子进程
自成进程组，便于整棵树强杀。
"""

from __future__ import annotations

import os
import subprocess
import sys

from ....foundation.logs import get_logger
from ...ports import SessionSpec, Stream
from ..errors import HostSpawnError
from ..process_tree import (
    CREATE_SUSPENDED,
    ProcessTree,
    assign_job,
    close_job,
    create_job,
    resume_process,
)

_logger = get_logger("core.runtime.subprocess.host")

_IS_WINDOWS = sys.platform == "win32"


def _wait_readable(fd: int, timeout: float) -> bool:
    """等 fd 可读或超时。"""
    if _IS_WINDOWS:
        return _windows_wait_readable(fd, timeout)
    import selectors

    with selectors.DefaultSelector() as selector:
        selector.register(fd, selectors.EVENT_READ)
        return bool(selector.select(timeout))


def _windows_wait_readable(fd: int, timeout: float) -> bool:  # pragma: no cover - 平台分支
    import ctypes
    import msvcrt
    from ctypes import wintypes

    handle = wintypes.HANDLE(msvcrt.get_osfhandle(fd))
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    # WAIT_OBJECT_0：管道有数据，或写端已关闭（此时让上层读到 EOF）
    return kernel32.WaitForSingleObject(handle, max(0, int(timeout * 1000))) == 0


def _read_pipe(stream, max_bytes: int, timeout: float | None) -> bytes:
    if stream is None:
        return b""
    fd = stream.fileno()
    if timeout is None:
        return os.read(fd, max_bytes)  # 阻塞；EOF 返回 b""
    if not _wait_readable(fd, timeout):
        return b""
    return os.read(fd, max_bytes)


class SubprocessHost:
    """子进程宿主。"""

    def __init__(self, spec: SessionSpec) -> None:
        env = dict(os.environ)
        env.update(spec.env)
        # 先建作业、再 spawn。Windows 上以挂起态创建，入作业后再恢复：进程在入作业
        # 前不执行任何指令，因此不可能已经 fork 出逃逸的孙进程。
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
            self._proc = subprocess.Popen(
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
                if not assign_job(job, self._proc.pid):
                    # 入作业失败就必须丢掉作业身份：留着它会让 kill 走作业分支（空作业 = 没杀）、
                    # descendants 误报空列表。退回按 pid 终止，观测由 MonitorUnavailable 报错。
                    _logger.warning(
                        "子进程加入作业对象失败，改用按 pid 终止 pid=%s", self._proc.pid
                    )
                    close_job(job)
                    job = None
                # 入作业成败都必须恢复，否则挂起的子进程永远不会运行
                resume_process(self._proc.pid)
            except Exception:
                self._proc.kill()  # 恢复失败会让子进程永远挂在挂起态，先收掉
                close_job(job)
                raise
        self._tree = ProcessTree(self._proc.pid, job)
        self._closed = False

    # ── HostLifecycle ──────────────────────────────────────────

    @property
    def pid(self) -> int | None:
        return None if self._closed else self._proc.pid

    def read(self, max_bytes: int = 65536, timeout: float | None = 0.2) -> bytes:
        if self._closed:  # 管道已关，`fileno()` 会抛
            return b""
        return _read_pipe(self._proc.stdout, max_bytes, timeout)

    def write(self, data: bytes) -> None:
        stdin = self._proc.stdin
        if stdin is None or self._closed:
            return
        try:
            stdin.write(data)
            stdin.flush()
        except (BrokenPipeError, ValueError):
            pass  # 子进程已关 stdin：不是错误

    def try_wait(self) -> int | None:
        if self._closed:
            return None
        return self._proc.poll()

    def poll_eof(self, stream: Stream = Stream.STDOUT) -> bool:
        """本路是否已排空：管道读空就是真 EOF，进程退出后不必再静默。

        退出后管道里剩下的字节仍然可读，读线程会先取走它们；等到一次读空，
        退出才等价于排空。
        """
        if self._closed:
            return True
        return self._proc.poll() is not None

    def kill(self) -> None:
        self._tree.kill()

    def descendants(self) -> tuple[int, ...]:
        """本会话进程树的当前成员（不含根进程）。"""
        return self._tree.descendants()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._tree.close()
        for pipe in (self._proc.stdin, self._proc.stdout, self._proc.stderr):
            if pipe is not None:
                try:
                    pipe.close()
                except OSError:
                    pass

    # ── ProcessHost ────────────────────────────────────────────

    def read_stderr(self, max_bytes: int = 65536, timeout: float | None = 0.2) -> bytes:
        if self._closed:
            return b""
        return _read_pipe(self._proc.stderr, max_bytes, timeout)

    def close_stdin(self) -> None:
        stdin = self._proc.stdin
        if stdin is None:
            return
        try:
            stdin.close()
        except OSError:
            pass
