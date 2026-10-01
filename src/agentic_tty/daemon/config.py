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

    name: str = "agentic-tty"
    """单实例锁名与运行时目录名。带命名空间，多用户与测试可以共存。"""
    listen: str = "tcp://127.0.0.1:0"
    """监听地址。端口写 0 = 让内核挑，实际地址从 `Daemon.address` 取。"""
    runtime_dir: Path | None = None
    """运行时目录（pid / 锁 / 端点 / 日志）；留空取平台默认。"""

    tick_interval: float = 0.005
    """所有者循环每轮之间的间隔。"""
    drain_timeout: float = 5.0
    """draining 阶段最多给在途命令多久跑完。"""
    stop_timeout: float = 10.0
    """整体收尾预算；超时就不再等，把剩下的交给入口处理。"""

    inbound_maxsize: int = 256
    """每连接的入站帧队列长度（满了读线程等待，背压传到对端 TCP 缓冲）。"""
    outbound_maxsize: int = 256
    """每连接的出站响应队列长度（满了说明这个客户端不读了）。"""
    accept_batch: int = 16
    """每轮最多接入几条新连接（公平性：不让接入饿死会话推进）。"""

    write_log_file: bool = True
    """是否同时写轮转日志文件。"""
    log_max_bytes: int = 4 << 20
    log_backups: int = 3
