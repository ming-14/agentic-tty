"""示例守护进程入口：装上演示请求处理层，起一个守护进程。

    python -m agentic_tty.example.daemon [--listen tcp://127.0.0.1:8765]

就绪后把**实际监听地址**写进运行时目录的端点文件，`example.client` 默认从那里读。

**强制退出在这一层做**：`Daemon.stop()` 带超时返回"是否停干净"，超时说明还有线程卡着
（多半是某个宿主关闭卡住了）——到这一步只能硬退，否则进程永远退不掉（架构设计 §12.4）。
库不替调用方决定进程死活，所以这个 `os._exit` 放在入口而不是 `Daemon` 里。
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Sequence

from ..daemon.config import DaemonConfig
from ..daemon.errors import DaemonError
from ..daemon.server import Daemon
from ..foundation.logs import configure, get_logger
from ..runtime.errors import DependencyMissing
from .service import ExampleService

_logger = get_logger("example.daemon")

_STARTUP_FAILED = 2
"""启动期失败（已有实例 / 缺依赖）的退出码。"""


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agentic_tty.example.daemon", description="示例守护进程")
    parser.add_argument("--listen", default="tcp://127.0.0.1:0", help="监听地址（端口 0 = 内核挑）")
    parser.add_argument("--name", default="agentic-tty-example", help="单实例锁名与运行时目录名")
    parser.add_argument("--no-log-file", action="store_true", help="只写 stderr，不落日志文件")
    args = parser.parse_args(argv)

    configure()
    config = DaemonConfig(name=args.name, listen=args.listen, write_log_file=not args.no_log_file)
    # 这个 lambda 要到 start() 里才被调用，那时 daemon 已经存在，能读到真实监听地址。
    service = ExampleService(listen=lambda: daemon.address)
    daemon = Daemon(config, lambda: service)

    try:
        daemon.start()
    except (DaemonError, DependencyMissing) as exc:
        _logger.error("启动失败: %s", exc)
        return _STARTUP_FAILED
    except Exception as exc:  # 启动已回滚，这里只负责报告
        _logger.exception("启动失败: %s", exc)
        return _STARTUP_FAILED

    _logger.info("示例守护进程已就绪 listen=%s 端点文件=%s", daemon.address, daemon.endpoint_path)
    try:
        daemon.run()
    except KeyboardInterrupt:
        _logger.info("收到键盘中断")

    if daemon.stop():
        return 0
    _logger.error("收尾超时，强制退出")
    logging.shutdown()
    os._exit(1)


if __name__ == "__main__":
    sys.exit(main())
