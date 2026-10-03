"""值表 → 配置对象：**校验 + 按默认值的类型转换**，各层共用这一处。

值可能来自命令行（字符串）、环境变量（字符串）或 TOML（真类型），所以类型转换统一收口在
这里；转不过去就报 `ConfigError`。**各层的配置对象因此只需声明字段**——认不出的键在这里
被拦下（拼错一个键静默失效最坑人）。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import fields
from typing import Any, TypeVar

from .errors import ConfigError

_T = TypeVar("_T")


def defaults_of(cls: type) -> dict[str, Any]:
    """字段名 → 默认值。它同时是**"认得的键"的集合**。"""
    return {field.name: field.default for field in fields(cls)}


def from_values(
    cls: type[_T],
    values: Mapping[str, Any],
    *,
    optional: Mapping[str, Callable[[str], Any]] | None = None,
) -> _T:
    """校验并转换 `values`，然后建对象。

    `optional` 给"默认值是 `None`"的字段一个显式转换（空串按"不给"处理）——否则没法从
    字符串推它该是 `Path` 还是 `str`。
    """
    defaults = defaults_of(cls)
    unknown = sorted(set(values) - set(defaults))
    if unknown:
        raise ConfigError(f"未知配置项: {', '.join(unknown)}")
    return cls(
        **{key: _coerce(key, value, defaults[key], optional) for key, value in values.items()}
    )


def _coerce(
    key: str, value: Any, default: Any, optional: Mapping[str, Callable[[str], Any]] | None
) -> Any:
    """按**默认值的类型**转一次。"""
    if default is None:
        converter = (optional or {}).get(key, str)
        return None if value is None or value == "" else converter(str(value))
    if isinstance(default, bool):
        return _to_bool(key, value)
    try:
        return type(default)(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{key} 的值不合法: {value!r}") from exc


def _to_bool(key: str, value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{key} 不是布尔值: {value!r}")
