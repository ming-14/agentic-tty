"""示例层的会话装配：把 example 的模式映射到会话与宿主。

模式标签由 example 自己定义（`ExampleMode`），core 只把 `SessionSpec.mode` 当开放
字符串透传、不做校验：

- `fake`       → `ProcessSession` + `FakeHost`：脚本化的假子进程（双流、无终端模型）。
- `pty`        → `TerminalSession` + `PtyHost`：runtime 的真 PTY 宿主。
- `subprocess` → `ProcessSession` + `SubprocessHost`：runtime 的真子进程宿主。

因此选 `pty` 永远得到真 PTY，假宿主只由 `fake` 模式产生。
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum

from ..core.ports import SessionSpec
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
    spec = SessionSpec(mode=mode.value, argv=tuple(argv))
    if mode is ExampleMode.PTY:
        return TerminalSession(
            new_uid(), spec, create_host, journal_budget_bytes=journal_budget_bytes
        )
    # fake 与 subprocess 都是子进程形态：双流、无终端模型，只有宿主来源不同
    host_factory = _fake_host_factory if mode is ExampleMode.FAKE else create_host
    return ProcessSession(new_uid(), spec, host_factory, journal_budget_bytes=journal_budget_bytes)
