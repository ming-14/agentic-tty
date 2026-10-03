"""守护进程侧的入口：装配 core ＋ 请求处理层 ＋ 接入点，跑所有者循环。

    cd src && python -m agentic_tty.example.daemon_test.server

一般不用手起它——台子的 `__main__.py` 会把它拉成子进程。**不依赖安装**：从 `src/` 起
（或给 `PYTHONPATH=src`）即可 import。

这就是"守护进程那个进程"的最小实现——**将来正式的入口也是这个形状**：构造请求处理层
（core 装在它里面）→ 交给 `Daemon`（它在 `start()` 里挂接入点）→ `run()`。

`Daemon` 装了 SIGINT / SIGTERM 处理器，所以客户端把它 `terminate()` 掉就能优雅收尾。
"""

from __future__ import annotations

import sys

from ...core.runtime.host_factory import check_dependencies
from ...daemon.config import DaemonConfig
from ...daemon.server import Daemon
from ...foundation.logs import configure, get_logger
from ...transport.pipe import pipe_address
from . import NAME, runtime_dir
from .handler import KernelHandler

_logger = get_logger("example.daemon_test.server")


def build_daemon() -> Daemon:
    """装配守护进程：请求处理层（core 在里面）＋ 接入点（配置给出管道名）。"""
    config = DaemonConfig(name=NAME, runtime_dir=runtime_dir(), listen=NAME)
    return Daemon(
        config,
        lambda: KernelHandler(listen=pipe_address(NAME, config.runtime_dir)),
        check_dependencies=check_dependencies,
    )


def main() -> int:
    configure()
    daemon = build_daemon()
    daemon.start()
    _logger.info("守护进程已就绪，等客户端连入")
    try:
        daemon.run()
    finally:
        daemon.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
