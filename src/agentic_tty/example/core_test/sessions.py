"""示例层的会话装配：模式 → (会话类, 宿主工厂)，交给 core 的注册表创建。

模式标签由 example 自己定义（`ExampleMode`，pty / subprocess 的值直接取自 core 的
内置标签），core 只把 `SessionSpec.mode` 当开放字符串透传：

- `fake`       → `ProcessSession` + `FakeHost`：脚本化的假子进程（双流、无终端模型）。
- `pty`        → `TerminalSession` + `PtyHost`：core.runtime 的真 PTY 宿主。
- `subprocess` → `ProcessSession` + `SubprocessHost`：core.runtime 的真子进程宿主。

会话类与宿主工厂**成对**注册，因此选 `pty` 永远得到真 PTY，假宿主只由 `fake` 模式产生。
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum

from ...core import ports
from ...core.errors import CoreError
from ...core.ports import HostFactory, SessionSpec
from ...core.process.session import ProcessSession
from ...core.runtime.host_factory import create_host
from ...core.session.registry import SessionKind, SessionRegistry
from ...core.terminal.session import TerminalSession
from .programs import PROGRAMS
from .runtime_fakehost import FakeHost

EXAMPLE_JOURNAL_BUDGET = 1 << 20
"""示例层用更小的日志预算，便于观察裁剪与重建。"""


class ExampleMode(StrEnum):
    """示例层的模式。"""

    FAKE = "fake"
    PTY = ports.PTY
    SUBPROCESS = ports.SUBPROCESS


def session_spec(mode: ExampleMode, argv: Sequence[str]) -> SessionSpec:
    """把示例层的模式与命令行拼成 core 的会话描述。"""
    return SessionSpec(mode=mode.value, argv=tuple(argv))


def _fake_host_factory(spec: SessionSpec) -> FakeHost:
    name = spec.argv[0] if spec.argv else ""
    program = PROGRAMS.get(name)
    if program is None:
        raise CoreError(f"未定义假程序 {name!r}（可选：{', '.join(sorted(PROGRAMS))}）")
    return FakeHost(spec, program)


def create_registry(
    *,
    host_factory: HostFactory = create_host,
    journal_budget_bytes: int = EXAMPLE_JOURNAL_BUDGET,
) -> SessionRegistry:
    """装配示例层的注册表：三种模式各自声明会话类与宿主工厂。

    只有 `fake` 带专属宿主工厂（假宿主）；另两种用 `host_factory`——默认是 core 自带的
    真宿主，测试可注入假宿主。
    """
    return SessionRegistry(
        host_factory,
        kinds={
            ExampleMode.FAKE: SessionKind(ProcessSession, _fake_host_factory),
            ExampleMode.PTY: SessionKind(TerminalSession),
            ExampleMode.SUBPROCESS: SessionKind(ProcessSession),
        },
        journal_budget_bytes=journal_budget_bytes,
    )
