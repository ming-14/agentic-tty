"""守护进程配置。

配置是显式对象、随装配注入，装配时一次定死，不做热重载（避免"一半新配置一半旧配置"
的中间态）。

**字段留在这里**——`config/` 只管"名字叫什么"和"值从哪来"，不认识守护进程有哪些字段。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

from ..config import ConfigError

_OPTIONAL: dict[str, Any] = {"runtime_dir": Path, "listen": str}
"""默认值是 `None` 的字段给个显式转换——空串按"不给"处理。"""


def _coerce(key: str, value: Any, default: Any) -> Any:
    """按**默认值的类型**转一次：值可能来自命令行（字符串）、环境变量（字符串）或 TOML。"""
    if default is None:
        converter = _OPTIONAL.get(key, str)
        return None if value is None or value == "" else converter(str(value))
    if isinstance(default, bool):
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in {"1", "true", "yes", "on"}:
            return True
        if text in {"0", "false", "no", "off"}:
            return False
        raise ConfigError(f"{key} 不是布尔值: {value!r}")
    try:
        return type(default)(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{key} 的值不合法: {value!r}") from exc


@dataclass(frozen=True, slots=True)
class DaemonConfig:
    """守护进程的装配参数。"""

    name: str = "default"
    """**实例名**：单实例锁名与运行时目录名都由它派生。多份配置、多用户靠它共存。"""
    runtime_dir: Path | None = None
    """运行时目录（锁 / 端点 / 日志）；留空取 `config.names.runtime_dir(name)`。"""

    listen: str | None = None
    """接入点用的**端点名**（`config.names.endpoint_name()` 会加上项目前缀）。**留空不挂监听**。

    与 `name` 分开：`name` 决定"哪个实例（锁与目录）"，`listen` 决定"哪个端点（管道）"。
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

    @classmethod
    def defaults(cls) -> dict[str, Any]:
        """字段名 → 默认值。**它也是"认得的键"的集合**（`config.resolve` 拿它校验）。"""
        return {field.name: field.default for field in fields(cls)}

    @classmethod
    def from_values(cls, values: Mapping[str, Any]) -> DaemonConfig:
        """从一份扁平值表建配置（键名 = 字段名）；认不出的键报 `ConfigError`。"""
        defaults = cls.defaults()
        unknown = sorted(set(values) - set(defaults))
        if unknown:
            raise ConfigError(f"未知配置项: {', '.join(unknown)}")
        return cls(**{key: _coerce(key, value, defaults[key]) for key, value in values.items()})
