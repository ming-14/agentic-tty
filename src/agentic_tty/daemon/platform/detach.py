"""后台化：把守护进程从"跟着终端走"变成"独立进程"。

前台不变（默认）。**要不要后台由入口决定**（它解析 `BACKGROUND_FLAG`）；这一层只管
"起一个新进程"，`detach()` 正常返回就是起来了，起不来抛 `DetachError`。

**要脱离的是三样，各自独立**：

1. **父进程组 / 会话**：POSIX 上 `setsid` 一并办到——起新会话，那个终端此后
   `Ctrl-C` / 挂断都送不到它。Windows 的对应物是 **不继承控制台**（`DETACHED_PROCESS`）。
2. **父进程**：父进程可以退出。POSIX 上子进程被 `init` 收养；Windows 上只要别留在父进程
   的作业里（作业的 `KILL_ON_JOB_CLOSE` 会连坐）——用 `CREATE_BREAKAWAY_FROM_JOB`。
3. **控制终端（stdio）**：三个流接到 `os.devnull`，从此不再碰需要 tty 的东西。

**两平台都走"起一个新的自己、本进程退场"这同一个形状**（POSIX `start_new_session=True`
即 `setsid`；Windows 带 `DETACHED_PROCESS`）。刻意**不走 `preexec_fn` ＋ `fork`**：那要在
"已经装好线程、持有原生句柄"的进程里 fork，是 `vendor/openpty` 里明确警告过的坑——fork
只保活当前线程，别的线程持有的锁可能永不释放。

**成功与否不在这里判断**：这一层只确认"起得来"，"服务得了"照旧看**连得上**（§6.1）。
"""

from __future__ import annotations

import os
import subprocess
import sys

from ...foundation.errors import AgenticTtyError
from ...foundation.logs import get_logger

_logger = get_logger("daemon.platform.detach")
_IS_WINDOWS = sys.platform == "win32"

BACKGROUND_FLAG = "--background"
"""后台化那个入口标志。**定义只此一处**——入口的 argparse 与本层都从这里取，
免得两边写岔了：那一岔不会报错，只会"说要后台、实际还在前台"。"""


class DetachError(AgenticTtyError):
    """后台化失败——父进程不该继续跑下去。"""


def detach(args: list[str]) -> None:
    """起一个独立的新进程跑同一个入口。

    `args` 是**已经摘掉 `BACKGROUND_FLAG` 的**入口参数——摘不在这一层做：入口本来就要
    解析一遍拿 `args.background`，再在这里按字符串找一次就是同一件事说两遍。

    起不来抛 `DetachError`——**绝不让一个"打算后台跑"的进程留在前台**。
    """
    try:
        child = _spawn(args)
    except OSError as exc:
        raise DetachError(f"后台化失败: {exc}") from exc
    _logger.info("守护进程已后台化 pid=%s", child.pid)


def _spawn(args: list[str]) -> subprocess.Popen:
    """按平台起那个新进程；两平台共用的 stdio / cwd 在这里一次配好。"""
    argv = [sys.executable, "-m", "agentic_tty.daemon", *args]
    common: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
        # 从当前目录起——`--cwd` 已在入口里 chdir 过，新进程继承它
        "cwd": os.getcwd(),
    }
    if not _IS_WINDOWS:
        return subprocess.Popen(argv, start_new_session=True, **common)
    return _spawn_windows(argv, common)


def _spawn_windows(argv: list[str], common: dict) -> subprocess.Popen:
    """Windows：先带 `CREATE_BREAKAWAY_FROM_JOB` 试，被作业挡住就退回不带它。

    父进程若困在一个禁止 breakaway 的作业里（受限的启动器、某些宿主都是这样），带这个
    标志会直接 `WinError 5 拒绝访问`——那不算异常，是一种已知的环境。退回之后"脱离终端
    与父进程"仍然成立，只是万一那个作业被关掉它会被连坐。

    刻意**不加** `CREATE_NEW_PROCESS_GROUP`：那是"更容易被 `Ctrl-Break` 打到"，
    与脱离控制台的目标相反。
    """
    breakaway = subprocess.DETACHED_PROCESS | subprocess.CREATE_BREAKAWAY_FROM_JOB
    try:
        return subprocess.Popen(argv, creationflags=breakaway, **common)
    except OSError:
        return subprocess.Popen(argv, creationflags=subprocess.DETACHED_PROCESS, **common)
