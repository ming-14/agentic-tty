"""示例层的宿主工厂。

- 命令**只有一个 token 且命中示例假程序**（`build` / `noisy` / `repl` / `tail` /
  `crash`）→ 假宿主，不启动真程序。
- 其余命令 → 运行时层的**真宿主**：`pty` 走 pywezterm，`subprocess` 走 `Popen`。
"""

from __future__ import annotations

from ..core.ports import HostLifecycle, SessionSpec
from ..runtime.host_factory import create_host
from .fake_host import FakeHost
from .programs import PROGRAMS


def make_host(spec: SessionSpec) -> HostLifecycle:
    """单 token 且命中示例假程序名才用假宿主；带参数的命令一律走真宿主。"""
    program = PROGRAMS.get(spec.argv[0]) if len(spec.argv) == 1 else None
    if program is not None:
        return FakeHost(spec, program)
    return create_host(spec)
