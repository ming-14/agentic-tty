"""守护进程验证台入口：**连一个已经在跑的守护进程**。

    # 终端 1：你自己起守护进程
    cd src && python -m agentic_tty.daemon --name daemon-test
    # 终端 2：台子连上去
    cd src && python -m agentic_tty.example.daemon_test --name daemon-test

**守护进程由用户启动，本格不碰它**——不拉子进程、不给它传参、不替它决定叫什么。名字是
**给进来的**（`--name`），地址自己算，连不上就重连（见 `gui.py` 的三态）。

**不依赖安装**：`agentic_tty` 就在 `src/` 下，从那里起（或给 `PYTHONPATH=src`）就能 import。
"""

from __future__ import annotations

import argparse
import sys

from ...foundation.logs import configure, get_logger
from . import address

_logger = get_logger("example.daemon_test")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m agentic_tty.example.daemon_test",
        description="连一个已经在跑的守护进程",
    )
    parser.add_argument("--name", required=True, help="守护进程的实例名（它启动时用的那个）")
    args = parser.parse_args(argv)

    configure()
    _logger.info("连 %s", address(args.name))
    from .gui import main as run_gui  # 拉 Tk 放在参数校验之后

    return run_gui(args.name)


if __name__ == "__main__":
    sys.exit(main())
