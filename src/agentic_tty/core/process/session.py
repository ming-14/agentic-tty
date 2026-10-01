"""子进程会话：双流、无终端模型。

stdout 与 stderr 各自维护独立日志，**不合并**——两个管道读到的先后取决于
调度，不是程序的真实写出顺序，合并等于伪造一个假的顺序。因此 offset 空间、
游标、裁剪、视图全部每流独立。
"""

from __future__ import annotations

from ..errors import CoreError
from ..journal import OutputJournal
from ..ports import HostFactory, ProcessHost, SessionMode, SessionSpec, Stream
from ..session.base import Session


class ProcessSession(Session):
    """子进程会话。"""

    def __init__(
        self,
        uid: str,
        spec: SessionSpec,
        host_factory: HostFactory,
        *,
        journal_budget_bytes: int,
    ) -> None:
        if spec.mode is not SessionMode.PROCESS:
            raise ValueError("ProcessSession 只接受 subprocess 模式")
        super().__init__(uid, spec, host_factory, journal_budget_bytes=journal_budget_bytes)
        self._err_journal = OutputJournal(journal_budget_bytes)

    @property
    def stderr_journal(self) -> OutputJournal:
        return self._err_journal

    def close_stdin(self) -> None:
        """关闭 stdin 发 EOF。"""
        self._process_host().close_stdin()

    # ── 子类接缝 ──────────────────────────────────────────────

    def _streams(self) -> tuple[Stream, ...]:
        return (Stream.STDOUT, Stream.STDERR)

    def _read_secondary(self, stream: Stream, timeout: float | None, max_bytes: int) -> bytes:
        return self._process_host().read_stderr(max_bytes, timeout)

    def _journal_for(self, stream: Stream) -> OutputJournal:
        return self._err_journal if stream is Stream.STDERR else self._journal

    def _process_host(self) -> ProcessHost:
        host = self.host
        if host is None:
            raise CoreError("会话未启动")
        return host  # type: ignore[return-value]
