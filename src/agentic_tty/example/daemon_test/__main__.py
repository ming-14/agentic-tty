"""守护进程验证台入口：**连一个已经在跑的守护进程**。

    # 终端 1：用户自己起守护进程
    cd src && python -m agentic_tty.daemon
    # 终端 2：台子只连
    cd src && python -m agentic_tty.example.daemon_test

**守护进程由用户启动，本格不碰它**——不拉子进程、不给它传参、不替它决定叫什么。

**要连谁 = 配置常量**（`config.ConsumerConfig.name`，与守护进程同一个常量）——**没有命令行
选项、不读环境变量、不读文件**。

**不依赖安装**：`agentic_tty` 就在 `src/` 下，从那里起（或给 `PYTHONPATH=src`）就能 import。
"""

from __future__ import annotations

import sys

from ...config import ConsumerConfig
from ...foundation.logs import configure, get_logger
from . import address

_logger = get_logger("example.daemon_test")


def main() -> int:
    configure()
    name = ConsumerConfig().name
    _logger.info("连 %s", address(name))
    from .gui import main as run_gui  # 拉 Tk 放在这里

    return run_gui(name)


if __name__ == "__main__":
    sys.exit(main())
