"""守护进程验证台入口：**连一个已经在跑的守护进程**。

    # 终端 1：用户自己起守护进程
    cd src && python -m agentic_tty.daemon --name daemon-test
    # 终端 2：台子只连（要连谁由配置给）
    cd src && python -m agentic_tty.example.daemon_test --name daemon-test

**守护进程由用户启动，本格不碰它**——不拉子进程、不给它传参、不替它决定叫什么。

**要连谁 = 配置**，与守护进程同一套来源与优先级（`config.resolve`）：

    命令行 `--name` > 环境变量 `AGENTIC_TTY_NAME` > 配置文件 `--config <toml>` > 默认 `default`

台子认得的配置项**只有 `name`**——别的一概不管。它读的是**自己那份**配置文件（守护进程那
份有它自己的字段，`resolve` 对认不出的键直接报错）。

**不依赖安装**：`agentic_tty` 就在 `src/` 下，从那里起（或给 `PYTHONPATH=src`）就能 import。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from ...config import resolve
from ...config.names import DEFAULT_INSTANCE
from ...foundation.logs import configure, get_logger
from . import address

_logger = get_logger("example.daemon_test")

_DEFAULTS = {"name": DEFAULT_INSTANCE}
"""台子认得的配置项——**只有"连谁"**。"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m agentic_tty.example.daemon_test",
        description="连一个已经在跑的守护进程",
        argument_default=argparse.SUPPRESS,  # 只收显式给出的项，别顶掉环境变量 / 文件
    )
    parser.add_argument("--name", help=f"守护进程的实例名（默认 {DEFAULT_INSTANCE}）")
    parser.add_argument("--config", help="TOML 配置文件")
    given = vars(parser.parse_args(argv))
    config_file = given.pop("config", None)

    name = str(
        resolve(
            _DEFAULTS,
            argv=given,
            environ=os.environ,
            file=Path(config_file) if config_file else None,
        )["name"]
    )

    configure()
    _logger.info("连 %s", address(name))
    from .gui import main as run_gui  # 拉 Tk 放在参数校验之后

    return run_gui(name)


if __name__ == "__main__":
    sys.exit(main())
