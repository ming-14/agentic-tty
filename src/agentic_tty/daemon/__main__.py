"""守护进程入口：`python -m agentic_tty.daemon [--cwd <目录>]`

实例名：`config.DEFAULT_INSTANCE`
端点名：同实例名（`config.endpoint_name()`）
运行时目录：`config.runtime_dir(实例名)`
cwd：`--cwd`；不给 = 继承当前目录
"""

from __future__ import annotations

import argparse
import os
import sys

from ..config import DEFAULT_INSTANCE, DaemonConfig
from .assembly import run


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m agentic_tty.daemon", description="起一个守护进程"
    )
    parser.add_argument("--cwd", help="从哪个目录跑；不给 = 继承当前目录")
    args = parser.parse_args(argv)

    if args.cwd:
        try:
            os.chdir(args.cwd)
        except OSError as exc:  # 目录不存在 / 没权限
            parser.error(f"--cwd 进不去: {exc}")

    # 端点名就用实例名（`DaemonConfig.listen` 的 `None` 是留给进程内嵌入 / 单测的"不挂监听"）
    return run(DaemonConfig(name=DEFAULT_INSTANCE, listen=DEFAULT_INSTANCE))


if __name__ == "__main__":
    sys.exit(main())
