"""守护进程配置。

配置是显式对象、随装配注入，装配时一次定死，不做热重载（避免"一半新配置一半旧配置"
的中间态）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class DaemonConfig:
    """守护进程的装配参数。"""

    name: str = "default"
    """**实例名**：单实例锁名与运行时目录名都由它派生。多份配置、多用户靠它共存。"""
    runtime_dir: Path | None = None
    """运行时目录（锁 / 端点 / 日志）；留空取 `default_runtime_dir(name)`。"""

    listen: str | None = None
    """接入点用的**端点名**（挂成 `pipe://agentic-tty-<它>`）。**留空不挂监听**。

    与 `name` 分开：`name` 决定"哪个实例（锁与目录）"，`listen` 决定"哪个端点（管道）"。
    端点名由 `transport` 加项目前缀，所以这里别自己写 `agentic-tty-`。
    """

    tick_interval: float = 0.005
    """所有者循环每轮之间的间隔。"""
    drain_timeout: float = 5.0
    """draining 阶段最多给在途命令多久跑完。"""
    stop_timeout: float = 10.0
    """整体收尾预算；超时就不再等，把剩下的交给入口处理。"""

    inbound_maxsize: int = 256
    """入站队列长度（消费者线程投递、所有者线程消费）；满了投递方等待，背压传回消费者。"""

    write_log_file: bool = True
    """是否同时写轮转日志文件。"""
    log_max_bytes: int = 4 << 20
    log_backups: int = 3
