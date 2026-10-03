"""守护进程的验证台：**纯消费者**——连守护进程，一切经接入点往返。

    cd src && python -m agentic_tty.example.daemon_test

**不依赖安装**：`agentic_tty` 就在 `src/` 下，从那里起（或给 `PYTHONPATH=src`）就能 import。

本格只 import 公共层（`protocol` / `transport`）与 `example/ui`——**不 import `core`，也不
import `daemon`**。守护进程由 `__main__.py` **按模块名字符串**拉成子进程（那是"操作者"的
动作，不是依赖），界面再连上去。
"""

from __future__ import annotations

from pathlib import Path

from ...foundation.paths import default_runtime_dir

NAME = "daemon-test"
"""要连的那个守护进程的**实例名**——运行时目录与（加前缀后的）端点名都由它派生。"""


def runtime_dir() -> Path:
    """那个守护进程的运行时目录：pid / 锁 / 端点都在它里面。"""
    return default_runtime_dir(NAME)
