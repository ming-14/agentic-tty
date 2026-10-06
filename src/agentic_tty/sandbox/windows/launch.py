"""`sandbox_pty` 的 Windows 实现：condrv 造伪终端 + winsandbox 起受限子进程。

winsandbox 用受限令牌（`WRITE_RESTRICTED` + 能力 SID 写白名单）起子进程，可写区只有
**工作区**与**每次会话私有的 temp**（`TMP`/`TEMP` 指向它，会话结束即回收），其余位置
一律拒绝。它的 `working_dir` 同时是工作区根与子进程 cwd，所以一个 `SessionSpec.cwd`
就够。

与自带路径的差别只有"子进程怎么起、树归谁管"：作业是 winsandbox 的（它建带
`KILL_ON_JOB_CLOSE` 的作业），所以强杀与成员枚举走它的 `Process`，不是 `ProcessTree`。
"""

from __future__ import annotations

import os
import subprocess

from ...core.ports import SessionSpec
from ...core.runtime.errors import HostSpawnError, MonitorUnavailable
from ...core.runtime.local_pty.console import Console, ConsoleLauncher, require_primitive
from .wsandbox import SandboxInstance, SandboxProcess, require_winsandbox


class SandboxTree:
    """沙箱进程树：强杀与成员枚举交给 winsandbox 的作业。

    `close()` 之后不再装作还能观测——沙箱实例一关，`query_process_list()` 只会返回空，
    静默当"确实没有子进程"会把"看不到"和"没有"混同（与 `ProcessTree` 同一条口径）。
    """

    def __init__(self, process: SandboxProcess, instance: SandboxInstance) -> None:
        self._process = process
        self._instance = instance
        self._root = process.pid
        self._closed = False

    def kill(self) -> None:
        """强杀整棵作业树；实例已关时无事可做（那时树已经被收掉了）。"""
        if self._closed:
            return
        self._process.terminate()

    def descendants(self) -> tuple[int, ...]:
        """作业里**除根进程外**的成员 pid（升序）。"""
        if self._closed:
            raise MonitorUnavailable("沙箱实例已关闭，无法枚举成员")
        return tuple(sorted(p for p in self._process.query_process_list() if p != self._root))

    def close(self) -> None:
        """关掉沙箱实例：收掉它的作业与授权（幂等）。"""
        if self._closed:
            return
        self._closed = True
        self._instance.shutdown()


def make_launcher(*, workspace_write: bool = True) -> ConsoleLauncher:
    """造一个受限 spawn 的 launcher（`workspace_write=False` 时工作区也只读；
    私有 temp 两档都照样给）。"""

    def launch(spec: SessionSpec) -> Console:
        winsandbox = require_winsandbox()
        # 本平台的 PTY 原语：这里要的是它"先造 PTY、不认子进程"那一半
        console = require_primitive().ConDrvPty(cols=spec.cols, rows=spec.rows)
        argv = list(spec.argv)
        workspace = os.path.abspath(spec.cwd) if spec.cwd else os.getcwd()
        # 实例先建：它起不来时伪终端还没造，一行都不用收
        instance: SandboxInstance = winsandbox.SandboxInstance()
        try:
            console.open()
            process: SandboxProcess = instance.start_process(
                command_line=subprocess.list2cmdline(argv),
                working_dir=workspace,
                workspace_write=workspace_write,
                hpcon=console.console,
                # env 是**追加**：同名键覆盖不掉宿主已有的值（winsandbox 把覆盖项接在
                # 宿主环境块之后，Windows 取值先到先得）。要真覆盖得先改 winsandbox。
                env=dict(spec.env),
            )
            console.adopt(process.pid)
        except Exception as exc:
            instance.shutdown()  # 起不来就把实例与伪终端一起收掉，不留半截会话
            console.close()
            raise HostSpawnError(f"启动 sandbox_pty 会话失败 {argv}: {exc}") from exc
        return Console(pty=console, pid=process.pid, tree=SandboxTree(process, instance))

    return launch
