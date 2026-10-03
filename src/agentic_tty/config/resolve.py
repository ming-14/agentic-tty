"""按优先级合并配置值：**命令行 > 环境变量 > 配置文件 > 默认值**。

命令行那一层由调用方用 `argparse` 的 `argument_default=SUPPRESS` 收集——**只覆盖显式给出
的项**（没给的键根本不出现在表里，因此不会把环境变量/文件里的值顶掉）。
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .errors import ConfigError
from .sources import env_values, file_values


def resolve(
    defaults: Mapping[str, Any],
    *,
    argv: Mapping[str, Any] | None = None,
    environ: Mapping[str, str] | None = None,
    file: Path | None = None,
) -> dict[str, Any]:
    """合并出最终值表；`defaults` 的键就是**认得的键**。

    认不出的键直接报错——拼错一个键静默失效最坑人。
    """
    layers: list[Mapping[str, Any]] = []
    if file is not None:
        layers.append(file_values(file))
    if environ is not None:
        layers.append(env_values(environ))
    if argv is not None:
        layers.append(argv)

    merged = dict(defaults)
    for layer in layers:
        for key, value in layer.items():
            if key not in defaults:
                raise ConfigError(f"未知配置项: {key}（认得的：{', '.join(sorted(defaults))}）")
            merged[key] = value
    return merged
