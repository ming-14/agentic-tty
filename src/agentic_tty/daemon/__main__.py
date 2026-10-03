"""守护进程入口：`python -m agentic_tty.daemon`

**名字是配置常量**（`config.DEFAULT_INSTANCE`）——**没有命令行选项、不读环境变量、不读
文件**：没有别的地方可配。要跑别的实例，改那个常量。

**它只由用户启动**：别的东西不许拉它、不许给它传参、不许替它决定叫什么。
"""

from __future__ import annotations

import sys

from ..config import DEFAULT_INSTANCE, DaemonConfig
from .assembly import run


def main() -> int:
    # 跑守护进程就该有端点：端点名就用实例名（`DaemonConfig.listen` 的 `None` 是留给
    # 进程内嵌入 / 单测的"不挂监听"）
    return run(DaemonConfig(name=DEFAULT_INSTANCE, listen=DEFAULT_INSTANCE))


if __name__ == "__main__":
    sys.exit(main())
