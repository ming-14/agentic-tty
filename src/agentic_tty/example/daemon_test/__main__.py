"""守护进程验证台入口：把守护进程作为**子进程**拉起，再开界面连上去。

    python -m agentic_tty.example.daemon_test

客户端与守护进程因此是**两个进程**，中间只有那条本机管道——这正是"消费者连守护进程"
的样子。关窗时把子进程 `terminate()` 掉：`Daemon` 装了 SIGTERM 处理器，POSIX 上会优雅
收尾；Windows 没有 SIGTERM（是硬杀），但会话进程由作业对象 `KILL_ON_JOB_CLOSE` 兜底。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from ...foundation.logs import configure, get_logger
from ...transport.pipe import pipe_address
from . import NAME, runtime_dir

_logger = get_logger("example.daemon_test")

_READY_TIMEOUT = 15.0
"""等守护进程写 pid 的上限。"""
_READY_POLL = 0.05


def _child_env() -> dict[str, str]:
    """子进程的环境：源码树里跑时把 `src` 补进 `PYTHONPATH`（装过就不用）。"""
    env = dict(os.environ)
    source_root = Path(__file__).resolve().parents[3]
    if (source_root / "agentic_tty").is_dir():
        env["PYTHONPATH"] = os.pathsep.join(
            [str(source_root), env.get("PYTHONPATH", "")]
        ).rstrip(os.pathsep)
    return env


def _wait_ready(server: subprocess.Popen) -> bool:
    """等守护进程写 pid（写 pid 就是"装好了"）；它先死了就直接放弃。"""
    deadline = time.monotonic() + _READY_TIMEOUT
    pid_path = runtime_dir() / "daemon.pid"
    while time.monotonic() < deadline:
        if pid_path.exists():
            return True
        if server.poll() is not None:
            return False
        time.sleep(_READY_POLL)
    return False


def main() -> int:
    configure()
    # 上一轮硬杀（Windows 没有 SIGTERM）会留下陈旧 pid，会让"就绪"误判。
    (runtime_dir() / "daemon.pid").unlink(missing_ok=True)
    server = subprocess.Popen(
        [sys.executable, "-m", "agentic_tty.example.daemon_test.server"],
        env=_child_env(),
        stdout=sys.stdout,
        stderr=sys.stderr,
    )
    try:
        if not _wait_ready(server):
            _logger.error("守护进程没起来（见上面的日志）")
            return 1
        from .gui import main as run_gui  # 起不来就不必拉 Tk

        return run_gui(pipe_address(NAME, runtime_dir()))
    finally:
        server.terminate()
        try:
            server.wait(timeout=10.0)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=5.0)


if __name__ == "__main__":
    sys.exit(main())
