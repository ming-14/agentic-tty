"""守护进程验证台入口：**连一个已经在跑的守护进程**。

    # 终端 1：起守护进程
    cd src && python -m agentic_tty.daemon
    # 终端 2：台子只连
    cd src && python -m agentic_tty.example.daemon_test_console

实例名：`config.ConsumerConfig().name`（与守护进程同一个常量）
端点名：`config.ConsumerConfig().endpoint`；不给 = 由实例名派生（`config.endpoint_name()`）
运行时目录：`config.runtime_dir(实例名)`
锁名：`config.lock_name(实例名, 运行时目录)`
"""

from __future__ import annotations

import sys

from ...config import ConsumerConfig
from ...foundation.logs import configure, get_logger
from . import address

_logger = get_logger("example.daemon_test_console")


def main() -> int:
    configure()
    config = ConsumerConfig()
    _logger.info("连 %s", address(config.name, config.endpoint))
    from .gui import main as run_gui  # 延迟导入：拉 Tk 要 resvg-py，不必替 import 本模块的人付

    return run_gui(config.name, config.endpoint)


if __name__ == "__main__":
    sys.exit(main())
