"""守护进程入口：`python -m agentic_tty.daemon [--cwd <目录>]`

**实例名是配置常量**（`config.DEFAULT_INSTANCE`）——它不是参数，**没有别的地方可配**。

**唯一的选项是 `--cwd`**：这个守护进程**从哪个目录跑**（它开的会话默认就在那儿）。不给 =
继承你敲命令时所在的目录。**它不是配置**——它是"这一次从哪儿起"。

**它只由用户启动**：别的东西不许拉它、不许给它传参、不许替它决定叫什么。
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
    parser.add_argument(
        "--cwd", help="从哪个目录跑（它开的会话默认就在那儿）；不给 = 继承当前目录"
    )
    args = parser.parse_args(argv)

    if args.cwd:
        try:
            os.chdir(args.cwd)
        except OSError as exc:  # 目录不存在 / 没权限——在这儿就报清楚，别带着错目录往下走
            parser.error(f"--cwd 进不去: {exc}")

    # 端点名就用实例名（`DaemonConfig.listen` 的 `None` 是留给进程内嵌入 / 单测的"不挂监听"）
    return run(DaemonConfig(name=DEFAULT_INSTANCE, listen=DEFAULT_INSTANCE))


if __name__ == "__main__":
    sys.exit(main())
