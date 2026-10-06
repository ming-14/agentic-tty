"""本机伪终端原语 → 一台**已经连上子进程**的伪终端。

终端侧（pyte 模型、视图、尺寸）在 `local_pty/host.py`；这里只管"造出 PTY、把子进程
绑上去、把它的进程树交出来"。`localpty` 与沙箱宿主共用这一层——沙箱只是把子进程换成
受限 spawn，所以子进程由谁起抽成了 `ConsoleLauncher` 接缝。
"""

from __future__ import annotations

import importlib
import sys
from collections.abc import Callable
from dataclasses import dataclass
from types import ModuleType
from typing import Protocol

from ...ports import SessionSpec
from ..errors import DependencyMissing, HostSpawnError
from ..process_tree import ProcessTree, Tree, close_job, create_job

_IS_WINDOWS = sys.platform == "win32"

_primitive: ModuleType | None = None


def require_primitive() -> ModuleType:
    """惰性导入本平台的 PTY 原语；不可用则抛 `DependencyMissing`。"""
    global _primitive
    if _primitive is None:
        from ..vendor import ensure_vendor_on_path

        ensure_vendor_on_path()
        name = "condrv" if _IS_WINDOWS else "openpty"
        try:
            _primitive = importlib.import_module(name)
        except Exception as exc:
            raise DependencyMissing(f"{name} 不可用（应在仓库 vendor/ 下）: {exc}") from exc
    return _primitive


def _pty_class() -> type:
    """本平台的 PTY 原语类。两个原语各用自己描述性的类名，分派只此一处。"""
    module = require_primitive()
    return module.ConDrvPty if _IS_WINDOWS else module.OpenPty


class PtyPrimitive(Protocol):
    """本机伪终端原语的 IO 面：读写、退出码、排空、尺寸（`condrv` / `openpty` 各一份）。

    原语还各自带这一层没有的动作：两个都有、签名差一个 `job_handle` 的 `spawn`，以及
    Windows 独有的 `open` / `console` / `adopt`（见 `condrv.ConDrvPty`）——只有起会话的
    调用方用得到。
    """

    def read(self, max_bytes: int = 65536, timeout: float | None = 0.2) -> bytes: ...

    def write(self, data: bytes) -> None: ...

    def try_wait(self) -> int | None: ...

    def poll_eof(self) -> bool: ...

    def resize(self, cols: int, rows: int) -> None: ...

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class Console:
    """一台连好子进程的伪终端：PTY 本体 + 子进程 pid + 整棵树的观测与终止。"""

    pty: PtyPrimitive
    pid: int
    tree: Tree


ConsoleLauncher = Callable[[SessionSpec], Console]
"""起一台连好子进程的伪终端。换掉它 = 换掉"子进程怎么起"（沙箱就是这么接进来的）。"""


def open_console(spec: SessionSpec) -> Console:
    """自带路径：原语自己起子进程（Windows 经作业列表在**创建时**即入作业）。"""
    # 下面用的是原语自带的 `spawn`——它在 `PtyPrimitive` 之外，所以不按那个面标
    pty = _pty_class()(cols=spec.cols, rows=spec.rows)
    argv = list(spec.argv)
    env = dict(spec.env)
    # 作业对象只有 Windows 有；Linux 侧进程树靠 /proc，原语不收句柄
    job = create_job()
    try:
        if job is None:
            pid = pty.spawn(argv, cwd=spec.cwd, env=env)
        else:
            pid = pty.spawn(argv, cwd=spec.cwd, env=env, job_handle=job)
    except Exception as exc:
        close_job(job)
        raise HostSpawnError(f"启动 localpty 会话失败 {argv}: {exc}") from exc
    return Console(pty=pty, pid=pid, tree=ProcessTree(pid, job))
