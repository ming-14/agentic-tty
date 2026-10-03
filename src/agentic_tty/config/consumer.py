"""消费者配置：**要连哪个守护进程**。

各消费者（客户端命令处理层 / web / mcp / CLI / 验证台）与守护进程之间都只是**连接**关系，
所以它们要配的第一件事就是"连谁"——实例名。别的字段各消费者自己往上加。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .names import DEFAULT_INSTANCE
from .values import defaults_of
from .values import from_values as _build


@dataclass(frozen=True, slots=True)
class ConsumerConfig:
    """消费者的连接参数。"""

    name: str = DEFAULT_INSTANCE
    """要连的守护进程**实例名**——运行时目录、锁名与端点名都由它派生。"""

    @classmethod
    def defaults(cls) -> dict[str, Any]:
        """字段名 → 默认值；也是"认得的键"的集合。"""
        return defaults_of(cls)

    @classmethod
    def from_values(cls, values: Mapping[str, Any]) -> ConsumerConfig:
        """从一份扁平值表建配置；认不出的键报 `ConfigError`。"""
        return _build(cls, values)
