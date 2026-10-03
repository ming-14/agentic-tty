"""守护进程的验证台：**纯消费者**——连一个已经在跑的守护进程，一切经接入点往返。

    # 终端 1：你自己起守护进程
    cd src && python -m agentic_tty.daemon
    # 终端 2：台子连上去
    cd src && python -m agentic_tty.example.daemon_test

**不依赖安装**：`agentic_tty` 就在 `src/` 下，从那里起（或给 `PYTHONPATH=src`）就能 import。

**本格只连、不启动**：只 import 公共层（`config` / `foundation` / `protocol` / `transport`）与
`example/ui`——不 import `core`，也不 import `daemon`，**更不拉起任何进程**。守护进程是独立的
进程，**只由它自己支配**；名字是**配置常量**，地址两端各算一次。

**三态**靠两个公共信号分出来（`config.constants` 给名字、`foundation.instance` 问在不在）：

| 状态 | 单实例锁 | 管道 |
|---|---|---|
| 未启动 | 空 | 连不上 |
| 正在初始化 | **被占** | 连不上 |
| 已连接 | 被占 | **连得上** |
"""

from __future__ import annotations

from ...config import endpoint_name, lock_name, runtime_dir
from ...transport.pipe import pipe_address


def address(instance: str) -> str:
    """那个实例的接入点地址——与守护进程那边算的是同一个。"""
    return pipe_address(endpoint_name(instance), runtime_dir(instance))


def lock(instance: str) -> str:
    """那个实例的单实例锁名——只读地问"它在不在"（`foundation.instance.is_held`）。"""
    return lock_name(instance, runtime_dir(instance))
