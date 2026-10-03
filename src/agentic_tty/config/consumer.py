"""消费者的配置——**常量**。

各消费者（客户端命令处理层 / web / mcp / CLI / 验证台）与守护进程之间都只是**连接**关系，
所以它们要配的第一件事就是"连谁"——实例名。它和守护进程那边是**同一个常量**。
"""

from __future__ import annotations

from dataclasses import dataclass

from .constants import DEFAULT_INSTANCE


@dataclass(frozen=True, slots=True)
class ConsumerConfig:
    """消费者的连接参数。"""

    name: str = DEFAULT_INSTANCE
    """要连的守护进程**实例名**——运行时目录、锁名与端点名都由它派生。"""
