"""守护进程入口：

    python -m agentic_tty.daemon [--name <实例名>] [--listen <管道名>] [--cwd <目录>] [--background]
                           [--sandbox-read-only]

实例名：`--name`；不给 = `config.DEFAULT_INSTANCE`——锁名 / 运行时目录 / 日志文件名都由它派生
端点名：`--listen` 给**完整管道名**（不加前缀）；不给 = `config.endpoint_name(实例名)`
运行时目录：`config.runtime_dir(实例名)`
cwd：`--cwd`；不给 = 继承当前目录
前台：默认；`--background` = 脱离终端在后台跑（父进程起完就退，服务由新进程做）
沙箱会话的可写档：`--sandbox-read-only` = 工作区也只读（私有 temp 两档都有）
"""

from __future__ import annotations

import argparse
import os
import sys

from ..config import DEFAULT_INSTANCE, DaemonConfig
from .assembly import run
from .platform.detach import BACKGROUND_FLAG, DetachError, detach

_PROG = "python -m agentic_tty.daemon"


def main(argv: list[str] | None = None) -> int:
    raw = sys.argv[1:] if argv is None else argv
    args = _parse(raw)

    if args.cwd:
        try:
            os.chdir(args.cwd)
        except OSError as exc:  # 目录不存在 / 没权限
            print(f"{_PROG}: --cwd 进不去: {exc}", file=sys.stderr)
            return 2

    # 后台化：当前进程被新进程接管，这一份到此为止（服务在新进程里）。
    # `--background` 要从重放的参数里摘掉，否则新进程读完参数又去后台化，一代一代繁殖。
    if args.background:
        try:
            detach([arg for arg in raw if arg != BACKGROUND_FLAG])
        except DetachError as exc:
            print(f"{_PROG}: {exc}", file=sys.stderr)
            return 1
        return 0

    return run(
        DaemonConfig(
            name=args.name,
            endpoint=args.listen,
            sandbox_workspace_write=not args.sandbox_read_only,
        )
    )


def _parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog=_PROG, description="起一个守护进程")
    parser.add_argument(
        "--name", default=DEFAULT_INSTANCE, help="实例名；定锁名 / 运行时目录 / 日志名"
    )
    parser.add_argument(
        "--listen", default=None, help="接入点的完整管道名（不加前缀）；不给 = 由实例名派生"
    )
    parser.add_argument("--cwd", help="从哪个目录跑；不给 = 继承当前目录")
    parser.add_argument(
        "--sandbox-read-only",
        action="store_true",
        help="沙箱会话的工作区也只读（不给 = 工作区可写）",
    )
    parser.add_argument(
        BACKGROUND_FLAG,
        action="store_true",
        help="在后台跑：脱离终端与进程组后交给新进程，本进程立刻退出",
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    sys.exit(main())
