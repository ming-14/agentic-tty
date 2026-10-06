"""子进程宿主：双管道，无终端模型。

会话由 `ProcessLauncher` 起——自带的那份在 `pipes.py`（`subprocess.Popen` + 作业），
沙箱换掉同一个可调用对象即可把受限 spawn 接进来，复用本宿主的全部生命周期与端口
语义。
"""

from __future__ import annotations

from ...ports import SessionSpec, Stream
from ..process_tree import Tree
from .pipes import ProcessLauncher, ProcessPrimitive, open_process


class SubprocessHost:
    """子进程宿主。"""

    def __init__(self, spec: SessionSpec, *, launch: ProcessLauncher = open_process) -> None:
        console = launch(spec)
        self._pipes: ProcessPrimitive = console.pipes
        self._pid = console.pid
        self._tree: Tree = console.tree
        self._closed = False

    # ── HostLifecycle ──────────────────────────────────────────

    @property
    def pid(self) -> int | None:
        return None if self._closed else self._pid

    def read(self, max_bytes: int = 65536, timeout: float | None = 0.2) -> bytes:
        return self._pipes.read(max_bytes, timeout)

    def write(self, data: bytes) -> None:
        if not self._closed:
            self._pipes.write(data)

    def try_wait(self) -> int | None:
        return self._pipes.try_wait()

    def poll_eof(self, stream: Stream = Stream.STDOUT) -> bool:
        """本路是否已排空：管道读空就是真 EOF，进程退出后不必再静默。

        退出后管道里剩下的字节仍然可读，读线程会先取走它们；等到一次读空，
        退出才等价于排空。
        """
        if self._closed:
            return True
        return self._pipes.try_wait() is not None

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
        self._pipes.close()

    # ── ProcessHost ────────────────────────────────────────────

    def read_stderr(self, max_bytes: int = 65536, timeout: float | None = 0.2) -> bytes:
        return self._pipes.read_stderr(max_bytes, timeout)

    def close_stdin(self) -> None:
        self._pipes.close_stdin()
