"""示例层的会话装配：模式 → (会话类, 宿主工厂)，交给 core 的注册表创建。

模式标签由 example 自己定义（`ExampleMode`，pty / subprocess / localpty 的值直接取自
core 的内置标签），core 只把 `SessionSpec.mode` 当开放字符串透传：

- `fake`       → `ProcessSession` + `FakeHost`：脚本化的假子进程（双流、无终端模型）。
- `pty`        → `TerminalSession` + `PtyHost`：pywezterm 宿主（PTY + 终端模型一体）。
- `localpty`   → `TerminalSession` + `LocalPtyHost`：平台原语（condrv / openpty）配 pyte。
- `subprocess` → `ProcessSession` + `SubprocessHost`：core.runtime 的真子进程宿主。

会话类与宿主工厂**成对**注册，因此选 `pty` 永远得到真 PTY，假宿主只由 `fake` 模式产生。
`create_runtime` 再把注册表与驱动工厂一起装进 `Runtime`——验证台要的就是这个。
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum

from ...core import ports
from ...core.errors import CoreError
from ...core.ports import HostFactory, SessionSpec
from ...core.process.session import ProcessSession
from ...core.runtime.bridge import Wakeup
from ...core.runtime.host_factory import create_host
from ...core.runtime.runner import SessionRunner
from ...core.runtime.runtime import RunnerFactory, Runtime
from ...core.session.base import Session
from ...core.session.registry import SessionKind, SessionRegistry
from ...core.terminal.session import TerminalSession
from .programs import PROGRAMS
from .runtime_fakehost import FakeHost

EXAMPLE_JOURNAL_BUDGET = 1 << 20
"""示例层用更小的日志预算，便于观察裁剪与重建。"""

EXAMPLE_INPUT_MAX_BYTES = 4 << 10
EXAMPLE_INPUT_HIGH_WATERMARK = 2 << 10
EXAMPLE_INPUT_LOW_WATERMARK = 512
"""示例层用更小的输入水位，便于观察 HOLD / REJECTED——默认那套（1 MiB / 256 KiB /
64 KiB）是给生产的，手敲键盘永远碰不到。"""


class ExampleMode(StrEnum):
    """示例层的模式。"""

    FAKE = "fake"
    PTY = ports.PTY
    LOCALPTY = ports.LOCALPTY
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
    """装配示例层的注册表：四种模式各自声明会话类与宿主工厂。

    只有 `fake` 带专属宿主工厂（假宿主）；其余用 `host_factory`——默认是 core 自带的
    真宿主，测试可注入假宿主。
    """
    return SessionRegistry(
        host_factory,
        kinds={
            ExampleMode.FAKE: SessionKind(ProcessSession, _fake_host_factory),
            ExampleMode.PTY: SessionKind(TerminalSession),
            ExampleMode.LOCALPTY: SessionKind(TerminalSession),
            ExampleMode.SUBPROCESS: SessionKind(ProcessSession),
        },
        journal_budget_bytes=journal_budget_bytes,
    )


def make_runner_factory(wakeup: Wakeup | None = None) -> RunnerFactory:
    """示例层的驱动工厂：给会话配**小水位**，好让 HOLD / REJECTED 在台子上碰得到。"""

    def make_runner(session: Session) -> SessionRunner:
        return SessionRunner(
            session,
            wakeup=wakeup,
            input_max_bytes=EXAMPLE_INPUT_MAX_BYTES,
            input_high_watermark=EXAMPLE_INPUT_HIGH_WATERMARK,
            input_low_watermark=EXAMPLE_INPUT_LOW_WATERMARK,
        )

    return make_runner


def create_runtime(
    registry: SessionRegistry | None = None, *, wakeup: Wakeup | None = None
) -> Runtime:
    """装配示例层的运行时：三种模式的注册表（可换）+ 小水位的驱动工厂。"""
    return Runtime(
        registry if registry is not None else create_registry(),
        runner_factory=make_runner_factory(wakeup),
    )
