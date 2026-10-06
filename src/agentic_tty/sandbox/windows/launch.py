"""沙箱子进程的启动：pty 走 condrv 伪终端，subprocess 走双管道。

两条路共用同一份 winsandbox 限制（受限令牌 + 能力 SID 写白名单 + 作业）与同一套
`SandboxTree`，差的只有 stdio 从哪来：

- `make_launcher()` → `sandbox_pty`：condrv 造 ConPTY，winsandbox 用 `hpcon` 把它交给
  子进程；终端侧（pyte 模型、渲染、尺寸）全由 `LocalPtyHost` 复用。
- `make_process_launcher()` → `sandbox_subprocess`：`pipe_stdio=True` 让库造三条匿名
  管道，返回的三个句柄包成 fd 交给 `SubprocessHost`——与 `subprocess` 完全同形。

winsandbox 用受限令牌（`WRITE_RESTRICTED` + 能力 SID 写白名单）起子进程，可写区只有
**工作区**与**每次会话私有的 temp**（`TMP`/`TEMP` 指向它，会话结束即回收），其余位置
一律拒绝。它的 `working_dir` 同时是工作区根与子进程 cwd，所以一个 `SessionSpec.cwd`
就够。

作业是 winsandbox 的（它建带 `KILL_ON_JOB_CLOSE` 的作业），所以强杀与成员枚举走它的
`Process`，不是 `ProcessTree`。
"""

from __future__ import annotations

import msvcrt
import os
import subprocess

from ...core.ports import SessionSpec
from ...core.runtime.errors import HostSpawnError, MonitorUnavailable
from ...core.runtime.local_pty.console import Console, ConsoleLauncher, require_primitive
from ...core.runtime.subprocess.pipes import ProcessConsole, ProcessLauncher, read_fd
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
                # env 按名替换：与宿主同名的项被覆盖掉（winsandbox 的 env 语义）
                env=dict(spec.env),
            )
            console.adopt(process.pid)
        except Exception as exc:
            instance.shutdown()  # 起不来就把实例与伪终端一起收掉，不留半截会话
            console.close()
            raise HostSpawnError(f"启动 sandbox_pty 会话失败 {argv}: {exc}") from exc
        return Console(pty=console, pid=process.pid, tree=SandboxTree(process, instance))

    return launch


class SandboxPipes:
    """winsandbox 给的三条管道，包成 fd 后与 `Popen` 的管道同形。

    句柄的所有权随 spawn 交给调用方（库不再关它们），包成 fd 之后就由 fd 负责关闭。
    """

    def __init__(self, process: SandboxProcess) -> None:
        self._process = process
        # O_BINARY 免得 CRT 在 Windows 上做换行翻译；fd 是这三条管道的唯一持有者
        self._stdin = msvcrt.open_osfhandle(process.stdin_handle, os.O_WRONLY | os.O_BINARY)
        self._stdout = msvcrt.open_osfhandle(process.stdout_handle, os.O_RDONLY | os.O_BINARY)
        self._stderr = msvcrt.open_osfhandle(process.stderr_handle, os.O_RDONLY | os.O_BINARY)
        self._closed = False

    def read(self, max_bytes: int = 65536, timeout: float | None = 0.2) -> bytes:
        if self._closed:
            return b""
        return read_fd(self._stdout, max_bytes, timeout)

    def read_stderr(self, max_bytes: int = 65536, timeout: float | None = 0.2) -> bytes:
        if self._closed:
            return b""
        return read_fd(self._stderr, max_bytes, timeout)

    def write(self, data: bytes) -> None:
        if self._closed:
            return
        try:
            os.write(self._stdin, data)
        except OSError:
            pass  # 子进程已关 stdin：不是错误

    def close_stdin(self) -> None:
        """关掉 stdin 写端 = 给子进程发 EOF。"""
        fd, self._stdin = self._stdin, -1
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass

    def try_wait(self) -> int | None:
        """非阻塞查退出码；实例已被收掉时返 None（"没有更多信息"，与关闭后同口径）。"""
        if self._closed:
            return None
        try:
            settled = self._process.poll_exit()
        except Exception:
            return None
        return None if settled is None else int(settled[0])

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for fd in (self._stdin, self._stdout, self._stderr):
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
        self._stdin = self._stdout = self._stderr = -1


def make_process_launcher(*, workspace_write: bool = True) -> ProcessLauncher:
    """造一个受限 spawn 的双管道 launcher（`workspace_write=False` 时工作区也只读；
    私有 temp 两档都照样给）。"""

    def launch(spec: SessionSpec) -> ProcessConsole:
        winsandbox = require_winsandbox()
        argv = list(spec.argv)
        workspace = os.path.abspath(spec.cwd) if spec.cwd else os.getcwd()
        # 实例先建：它起不来时一行都不用收
        instance: SandboxInstance = winsandbox.SandboxInstance()
        try:
            process: SandboxProcess = instance.start_process(
                command_line=subprocess.list2cmdline(argv),
                working_dir=workspace,
                workspace_write=workspace_write,
                # env 按名替换：与宿主同名的项被覆盖掉（winsandbox 的 env 语义）
                env=dict(spec.env),
                pipe_stdio=True,
            )
            pipes = SandboxPipes(process)
        except Exception as exc:
            instance.shutdown()  # 起不来（或句柄包不成 fd）就把实例一起收掉
            raise HostSpawnError(f"启动 sandbox_subprocess 会话失败 {argv}: {exc}") from exc
        return ProcessConsole(pipes=pipes, pid=process.pid, tree=SandboxTree(process, instance))

    return launch
