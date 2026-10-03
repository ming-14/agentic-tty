"""守护进程验证台入口：**连一个已经在跑的守护进程**。

    # 终端 1：用户自己起守护进程
    cd src && python -m agentic_tty.daemon --name daemon-test
    # 终端 2：台子只连（要连谁由配置给）
    cd src && python -m agentic_tty.example.daemon_test --name daemon-test

**守护进程由用户启动，本格不碰它**——不拉子进程、不给它传参、不替它决定叫什么。

**要连谁 = 配置**，与守护进程同一套来源（`config.resolve`）：

    命令行 `--name` > 环境变量 `AGENTIC_TTY_NAME` > 默认 `default`

台子认得的配置项**只有 `name`**——别的一概不管。

**不依赖安装**：`agentic_tty` 就在 `src/` 下，从那里起（或给 `PYTHONPATH=src`）就能 import。
"""

from __future__ import annotations

import argparse
import os
import sys

from ...config import ConsumerConfig, resolve
from ...foundation.logs import configure, get_logger
from . import address

_logger = get_logger("example.daemon_test")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m agentic_tty.example.daemon_test",
        description="连一个已经在跑的守护进程",
        argument_default=argparse.SUPPRESS,  # 只收显式给出的项，别顶掉环境变量
    )
    parser.add_argument("--name", help=f"守护进程的实例名（默认 {ConsumerConfig().name}）")
    given = vars(parser.parse_args(argv))

    config = ConsumerConfig.from_values(
        resolve(ConsumerConfig.defaults(), argv=given, environ=os.environ)
    )

    configure()
    _logger.info("连 %s", address(config.name))
    from .gui import main as run_gui  # 拉 Tk 放在参数校验之后

    return run_gui(config.name)


if __name__ == "__main__":
    sys.exit(main())
