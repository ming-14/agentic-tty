"""守护进程的配置——字段默认值就是 `constants` 里的常量，入口可以覆盖实例名与端点名。

装配参数随装配注入的显式对象，装配时一次定死，不做热重载（避免"一半新配置一半旧配置"
的中间态）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .constants import DEFAULT_INSTANCE


@dataclass(frozen=True, slots=True)
class DaemonConfig:
    """守护进程的装配参数。"""

    name: str = DEFAULT_INSTANCE
    """**实例名**：单实例锁名、运行时目录名、日志文件名都由它派生。"""
    endpoint: str | None = None
    """接入点的**完整名字**（`pipe://` 的 netloc，不加项目前缀）；留空取 `endpoint_name(name)`。"""
    directory: Path | None = None
    """运行时目录（锁 / 端点 / 日志）；留空取 `config.runtime_dir(name)`。"""

    mount_endpoint: bool = True
    """是否挂接入点。`False` 留给进程内嵌入 / 单测——它们不经本机管道接入。"""

    tick_interval: float = 0.005
    """接入点最多隔多久被轮询一次；会话输出会立刻唤醒循环，不受它限制。"""
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
