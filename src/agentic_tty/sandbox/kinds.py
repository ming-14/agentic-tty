"""沙箱模式：标签与平台分派。

与 `localpty` 同构——平台中立的标签，实现按平台选（Windows 现在有，Linux 将来只多
一个 `linux/` 与一条分支）。两个标签各复用一份现成的宿主，换掉的只有"子进程怎么起"：

- `sandbox_pty` 复用 `LocalPtyHost` 的整个终端侧（pyte 模型、渲染、尺寸）；
- `sandbox_subprocess` 复用 `SubprocessHost` 的双管道语义。

沙箱参数是**部署级**的：哪些会话可写工作区由装配方定，所以由工厂闭包带进去，
`SessionSpec` 一个字段都不用加。
"""

from __future__ import annotations

import sys

from ..core.ports import HostFactory, HostLifecycle, SessionSpec
from ..core.process.session import ProcessSession
from ..core.runtime.errors import DependencyMissing
from ..core.session.registry import SessionKind
from ..core.terminal.session import TerminalSession

SANDBOX_PTY = "sandbox_pty"
"""受限 spawn 的伪终端会话：终端语义与 `localpty` 完全一致（同一个模型与渲染），
差别只在子进程受写限制约束。"""

SANDBOX_SUBPROCESS = "sandbox_subprocess"
"""受限 spawn 的双管道会话：字节流语义与 `subprocess` 完全一致（同样有 stderr 与
`close_stdin`），差别只在子进程受写限制约束。"""


def _require_windows() -> None:
    """沙箱实现目前只有 Windows 一份；别的平台在建会话时报缺依赖，不静默降级。"""
    if sys.platform != "win32":
        raise DependencyMissing("沙箱目前只有 Windows 实现（见 vendor/README.md）")


def pty_host_factory(*, workspace_write: bool = True) -> HostFactory:
    """`sandbox_pty` 的宿主工厂。

    两档都拿到一个**私有的可写 temp**（`TMP`/`TEMP` 指向它，进程退出即回收）：宿主
    temp 根目录在写白名单之外，拿不到可写 temp 的进程（DLL 初始化、解释器）起不来。
    `workspace_write=False` 关掉的只有工作区本身。
    """

    def make_host(spec: SessionSpec) -> HostLifecycle:
        _require_windows()
        # 两样都惰性导入（与 `core.runtime.host_factory` 同一条口径）：不跑沙箱的进程
        # 不该被 pyte 或 winsandbox 拖住——装配处只 import 本模块而已。
        from ..core.runtime.local_pty.host import LocalPtyHost
        from .windows.launch import make_launcher

        return LocalPtyHost(spec, launch=make_launcher(workspace_write=workspace_write))

    return make_host


def process_host_factory(*, workspace_write: bool = True) -> HostFactory:
    """`sandbox_subprocess` 的宿主工厂。"""

    def make_host(spec: SessionSpec) -> HostLifecycle:
        _require_windows()
        from ..core.runtime.subprocess.host import SubprocessHost
        from .windows.launch import make_process_launcher

        return SubprocessHost(spec, launch=make_process_launcher(workspace_write=workspace_write))

    return make_host


def sandbox_kinds(*, workspace_write: bool = True) -> dict[str, SessionKind]:
    """沙箱的 `标签 → 形态`。

    装配处要与 `DEFAULT_KINDS` **合并**后再交给注册表——注册表的 `kinds` 是替换，
    不是合并，直接传会丢掉内置那三种。
    """
    return {
        SANDBOX_PTY: SessionKind(
            TerminalSession, pty_host_factory(workspace_write=workspace_write)
        ),
        SANDBOX_SUBPROCESS: SessionKind(
            ProcessSession, process_host_factory(workspace_write=workspace_write)
        ),
    }
