"""守护进程的验证台：一个守护进程 ＋ 一个客户端，两个进程，走真管道。

    cd src && python -m agentic_tty.example.daemon_test

**不依赖安装**：`agentic_tty` 就在 `src/` 下，从那里起（或给 `PYTHONPATH=src`）就能 import。

两侧：

- **守护进程侧**（`handler.py` / `server.py`）——碰 core：请求处理层与接入点。
- **客户端侧**（`client.py` / `gui.py`）——只碰 `protocol` / `transport` 与 `example/ui`。

`__main__.py` 把守护进程作为**子进程**拉起，再开 Tk 界面连上去——这正是"消费者连守护
进程"的样子，而不是把它装进自己的进程。
"""

from __future__ import annotations

from pathlib import Path

from ...foundation.paths import default_runtime_dir

NAME = "agentic-tty-daemon-test"
"""守护进程名：单实例锁、运行时目录、接入点管道名都用它。"""


def runtime_dir() -> Path:
    """两端共用的运行时目录——pid / 锁 / 端点都落在它里面。"""
    return default_runtime_dir(NAME)
