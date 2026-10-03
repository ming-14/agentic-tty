"""命令行入口：`python -m agentic_tty.daemon [选项]`

    --name <实例名>        实例名（锁 + 运行时目录）；环境变量 `AGENTIC_TTY_NAME`
    --listen <端点名>      接入点端点名（默认同实例名）；`AGENTIC_TTY_LISTEN`
    --runtime-dir <目录>   运行时目录；`AGENTIC_TTY_RUNTIME_DIR`

命令行**只覆盖显式给出的项**——`argument_default=SUPPRESS` 保证没给的键根本不出现。
"""

from __future__ import annotations

import argparse
import os
import sys

from ..config import DaemonConfig, resolve
from .assembly import run


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m agentic_tty.daemon",
        description="起一个守护进程",
        argument_default=argparse.SUPPRESS,
    )
    parser.add_argument("--name", help="实例名（默认 %(default)s）")
    parser.add_argument("--listen", help="接入点端点名（默认同实例名）")
    parser.add_argument("--runtime-dir", dest="runtime_dir", help="运行时目录")
    values = resolve(
        DaemonConfig.defaults(),
        argv=vars(parser.parse_args(argv)),
        environ=os.environ,
    )
    if values.get("listen") is None:
        # 跑守护进程就该有端点：没显式给就用**实例名**当端点名（`DaemonConfig.listen` 的
        # `None` 是留给进程内嵌入 / 单测的"不挂监听"，不是命令行该有的默认）
        values["listen"] = values["name"]
    return run(DaemonConfig.from_values(values))


if __name__ == "__main__":
    sys.exit(main())
