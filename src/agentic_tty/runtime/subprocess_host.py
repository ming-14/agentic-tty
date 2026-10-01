"""子进程宿主：`Popen` 双管道，无终端模型。

`start_new_session`（POSIX）/ `CREATE_NEW_PROCESS_GROUP`（Windows）让子进程
自成进程组，便于整棵树强杀。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

from ..core.ports import HostMetadata, SessionSpec
from .errors import HostSpawnError
from .process_tree import ProcessTree

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
        extra: dict = {}
        if _IS_WINDOWS:
            extra["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
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
            raise HostSpawnError(f"启动子进程失败 {list(spec.argv)}: {exc}") from exc
        self._tree = ProcessTree(self._proc.pid)
        self._closed = False

    # ── HostLifecycle ──────────────────────────────────────────

    @property
    def pid(self) -> int | None:
        return None if self._closed else self._proc.pid

    def read(self, max_bytes: int = 65536, timeout: float | None = 0.2) -> bytes:
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
        return self._proc.poll()

    def kill(self) -> None:
        self._tree.kill()

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
        return _read_pipe(self._proc.stderr, max_bytes, timeout)

    def close_stdin(self) -> None:
        stdin = self._proc.stdin
        if stdin is None:
            return
        try:
            stdin.close()
        except OSError:
            pass

    # ── 观测 ───────────────────────────────────────────────────

    def metadata(self) -> HostMetadata:
        return HostMetadata()

    def wait_exit(self, timeout: float) -> int | None:
        """带超时地等退出码（供测试与收尾使用）。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            code = self._proc.poll()
            if code is not None:
                return code
            time.sleep(0.01)
        return None
