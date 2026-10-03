"""守护进程的验证台：**纯消费者**——连守护进程，一切经接入点往返。

    cd src && python -m agentic_tty.example.daemon_test

**不依赖安装**：`agentic_tty` 就在 `src/` 下，从那里起（或给 `PYTHONPATH=src`）就能 import。

本格只 import 公共层（`config` / `foundation` / `protocol` / `transport`）与 `example/ui`——
**不 import `core`，也不 import `daemon`**。守护进程由 `__main__.py` **按模块名字符串**拉成
子进程（那是"操作者的动作"，不是依赖），界面再连上去。

**三态**靠两个公共信号分出来（`config.names` 给名字、`foundation.instance` 问在不在）：

| 状态 | 单实例锁 | 管道 |
|---|---|---|
| 未启动 | 空 | 连不上 |
| 正在初始化 | **被占** | 连不上 |
| 已连接 | 被占 | **连得上** |
"""

from __future__ import annotations

from ...config.names import lock_name, runtime_dir

NAME = "daemon-test"
"""要连的那个守护进程的**实例名**——运行时目录、锁名与端点名都由它派生。"""


def lock() -> str:
    """那个守护进程的单实例锁名。配合 `foundation.instance.is_held()` 只读地问"它在不在"。"""
    return lock_name(NAME, runtime_dir(NAME))
