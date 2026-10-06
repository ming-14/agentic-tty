"""消费者的配置——字段默认值就是 `constants` 里的常量，调用方可以覆盖实例名与端点名。

各消费者与守护进程之间只是**连接**关系，所以要配的第一件事是"连谁"——实例名（定运行时
目录）与端点名（定接入点）。它们和守护进程那边用的是同一套名字。
"""

from __future__ import annotations

from dataclasses import dataclass

from .constants import DEFAULT_INSTANCE


@dataclass(frozen=True, slots=True)
class ConsumerConfig:
    """消费者的连接参数。"""

    name: str = DEFAULT_INSTANCE
    """要连的守护进程**实例名**——运行时目录与锁名由它派生。"""
    endpoint: str | None = None
    """要连的**完整端点名**（`pipe://` 的 netloc）；留空取 `endpoint_name(name)`。"""
