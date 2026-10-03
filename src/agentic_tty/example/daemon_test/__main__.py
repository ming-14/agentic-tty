"""守护进程验证台入口：把守护进程作为**子进程**拉起，再开界面连上去。

    cd src && python -m agentic_tty.example.daemon_test

**不依赖安装**：`agentic_tty` 就在 `src/` 下，从那里起（或给 `PYTHONPATH=src`）就能 import；
拉起的子进程由 `_child_env()` 自己把 `src` 补进 `PYTHONPATH`，所以子进程不受 cwd 影响。

**本格是消费者**：守护进程按**模块名字符串**拉起（`python -m agentic_tty.daemon --name …`），
不 import 它——那是"操作者的动作"，不是依赖。关窗时把子进程 `terminate()` 掉：`Daemon` 装了
SIGTERM 处理器，POSIX 上优雅收尾；Windows 没有 SIGTERM（是硬杀），会话进程由作业对象兜底。

**就绪 = 连得上**：界面自己重试、自己分三态（见 `gui.py`），这里不等任何文件。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from ...config.names import endpoint_name, runtime_dir
from ...foundation.logs import configure, get_logger
from ...transport.pipe import pipe_address
from . import NAME

_logger = get_logger("example.daemon_test")

_DAEMON_MODULE = "agentic_tty.daemon"
"""守护进程的模块名。**只是个字符串**——拉起它不等于 import 它。"""


def _child_env() -> dict[str, str]:
    """子进程的环境：把 `src` 补进 `PYTHONPATH`，于是子进程不受 cwd 影响。"""
    env = dict(os.environ)
    source_root = Path(__file__).resolve().parents[3]
    if (source_root / "agentic_tty").is_dir():
        env["PYTHONPATH"] = os.pathsep.join(
            [str(source_root), env.get("PYTHONPATH", "")]
        ).rstrip(os.pathsep)
    return env


def main() -> int:
    configure()
    address = pipe_address(endpoint_name(NAME), runtime_dir(NAME))
    server = subprocess.Popen(
        [sys.executable, "-m", _DAEMON_MODULE, "--name", NAME],
        env=_child_env(),
        stdout=sys.stdout,
        stderr=sys.stderr,
    )
    try:
        from .gui import main as run_gui

        return run_gui(address)
    finally:
        server.terminate()
        try:
            server.wait(timeout=10.0)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=5.0)


if __name__ == "__main__":
    sys.exit(main())
