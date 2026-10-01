"""示例层的会话装配：把 example 的模式映射到会话与宿主。

example 有**自己的模式标签**（`ExampleMode`），core 只把 `SessionSpec.mode` 当开放
字符串透传，不做校验：

- `fake`       → `FakeSession` + `FakeHost`：example 自有的假会话，core 不感知。
- `pty`        → `TerminalSession` + `PtyHost`：runtime 的真 PTY 宿主。
- `subprocess` → `ProcessSession` + `SubprocessHost`：runtime 的真子进程宿主。

因此选 `pty` 永远得到真 PTY，假宿主只由 `fake` 模式产生。
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum

from ..core.errors import CoreError
from ..core.journal import OutputJournal
from ..core.ports import HostFactory, SessionSpec, Stream
from ..core.process.session import ProcessSession
from ..core.session.base import Session
from ..core.terminal.session import TerminalSession
from ..foundation.ids import new_uid
from ..runtime.host_factory import create_host
from .fake_host import FakeHost
from .programs import PROGRAMS

DEFAULT_JOURNAL_BUDGET = 1 << 20


class ExampleMode(StrEnum):
    """示例层的模式。"""

    FAKE = "fake"
    PTY = "pty"
    SUBPROCESS = "subprocess"


class FakeSession(Session):
    """示例层的假会话：双流 + 屏幕。

    假宿主既提供终端模型（`ingest` / `snapshot`）又提供双管道（`read_stderr`），
    所以假会话比任一真会话都宽——假程序库同时覆盖终端型（`repl`）与管道型
    （`build` / `noisy`）程序，一批假程序就能把两种输出形态都演示到。

    `spec.mode` 就是 example 自己的标签 `"fake"`——core 把它当开放字符串透传、
    不做校验（日志里因此显示 `mode=fake`）。
    """

    def __init__(
        self,
        uid: str,
        spec: SessionSpec,
        host_factory: HostFactory,
        *,
        journal_budget_bytes: int,
    ) -> None:
        super().__init__(uid, spec, host_factory, journal_budget_bytes=journal_budget_bytes)
        self._err_journal = OutputJournal(journal_budget_bytes)
        self._cols = spec.cols
        self._rows = spec.rows

    def snapshot(self) -> bytes:
        """屏幕快照：假宿主为已摄入的纯文本尾部。"""
        return self._fake_host().snapshot()

    @property
    def cols(self) -> int:
        return self._cols

    @property
    def rows(self) -> int:
        return self._rows

    def resize(self, cols: int, rows: int) -> None:
        self._fake_host().resize(cols, rows)
        self._cols, self._rows = cols, rows

    # ── 子类接缝 ──────────────────────────────────────────────

    def _streams(self) -> tuple[Stream, ...]:
        return (Stream.STDOUT, Stream.STDERR)

    def _feed_model(self, data: bytes, stream: Stream) -> bytes:
        if stream is not Stream.STDOUT:
            return b""  # stderr 不进屏幕
        return self._fake_host().ingest(data)

    def _read_secondary(self, stream: Stream, timeout: float | None, max_bytes: int) -> bytes:
        return self._fake_host().read_stderr(max_bytes, timeout)

    def _journal_for(self, stream: Stream) -> OutputJournal:
        return self._err_journal if stream is Stream.STDERR else self._journal

    def _fake_host(self) -> FakeHost:
        host = self.host
        if host is None:
            raise CoreError("会话未启动")
        return host  # type: ignore[return-value]


def _fake_host_factory(spec: SessionSpec) -> FakeHost:
    name = spec.argv[0] if spec.argv else ""
    program = PROGRAMS.get(name)
    if program is None:
        raise KeyError(f"未定义假程序 {name!r}（可选：{', '.join(sorted(PROGRAMS))}）")
    return FakeHost(spec, program)


def create_session(
    mode: ExampleMode,
    argv: Sequence[str],
    *,
    journal_budget_bytes: int = DEFAULT_JOURNAL_BUDGET,
) -> Session:
    """按 example 模式装配会话（未启动）。"""
    args = tuple(argv)
    if mode is ExampleMode.FAKE:
        spec = SessionSpec(mode=mode.value, argv=args)
        return FakeSession(
            new_uid(), spec, _fake_host_factory, journal_budget_bytes=journal_budget_bytes
        )
    cls = TerminalSession if mode is ExampleMode.PTY else ProcessSession
    return cls(
        new_uid(),
        SessionSpec(mode=mode.value, argv=args),
        create_host,
        journal_budget_bytes=journal_budget_bytes,
    )
