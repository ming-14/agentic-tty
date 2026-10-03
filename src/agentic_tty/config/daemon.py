"""守护进程的配置——常量。

装配参数就是这些默认值：随装配注入的显式对象，装配时一次定死，不做热重载（避免"一半新
配置一半旧配置"的中间态）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .constants import DEFAULT_INSTANCE


@dataclass(frozen=True, slots=True)
class DaemonConfig:
    """守护进程的装配参数。"""

    name: str = DEFAULT_INSTANCE
    """**实例名**：单实例锁名与运行时目录名都由它派生。"""
    runtime_dir: Path | None = None
    """运行时目录（锁 / 端点 / 日志）；留空取 `config.runtime_dir(name)`。"""

    listen: str | None = None
    """接入点用的**端点名**（`config.endpoint_name()` 会加上项目前缀）。**留空不挂监听**。

    `None` 是留给进程内嵌入 / 单测的"不挂监听"；跑守护进程时入口会给它填上实例名。
    """

    tick_interval: float = 0.005
    """所有者循环每轮之间的间隔。"""
    drain_timeout: float = 5.0
    """draining 阶段最多给在途命令多久跑完。"""
    stop_timeout: float = 10.0
    """整体收尾预算；超时就不再等，把剩下的交给装配层处理。"""

    inbound_maxsize: int = 256
    """入站队列长度（消费者线程投递、所有者线程消费）；满了投递方等待，背压传回消费者。"""

    write_log_file: bool = True
    """是否同时写轮转日志文件。"""
    log_max_bytes: int = 4 << 20
    log_backups: int = 3
