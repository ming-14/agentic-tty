"""命令行入口：`python -m agentic_tty.daemon --name <实例名>`。

`--name` 是**实例名**：单实例锁与运行时目录由它派生；接入点管道名是 `transport` 加了
项目前缀之后的 `pipe://agentic-tty-<名字>`。
"""

from __future__ import annotations

import argparse
import sys

from .assembly import DEFAULT_NAME, run


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m agentic_tty.daemon", description="起一个守护进程"
    )
    parser.add_argument("--name", default=DEFAULT_NAME, help="实例名（默认 %(default)s）")
    return run(parser.parse_args(argv).name)


if __name__ == "__main__":
    sys.exit(main())
