"""配置值的来源：命令行 / 环境变量 / 文件。

三个函数各交出一份**扁平表**（键名与各层字段名一致），合并规则在 `resolve.py`。
**这里不认识任何一层的字段**，也不做类型转换——那是各层自己的事。
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .errors import ConfigError

ENV_PREFIX = "AGENTIC_TTY_"
"""环境变量的前缀：`AGENTIC_TTY_RUNTIME_DIR` → `runtime_dir`。"""


def env_values(
    environ: Mapping[str, str], *, prefix: str = ENV_PREFIX
) -> dict[str, str]:
    """把带前缀的环境变量收成扁平表；键名去掉前缀并转小写。"""
    values: dict[str, str] = {}
    for key, value in environ.items():
        if not key.startswith(prefix):
            continue
        name = key[len(prefix) :].lower()
        if name:
            values[name] = value
    return values


def file_values(path: Path) -> dict[str, Any]:
    """TOML 文件 → 扁平表。**只读**：`tomllib` 是标准库，写 TOML 不是。

    只认**顶层标量**——嵌套表留给各层自己解释（这里不做领域判断）。键名里的 `-` 换成 `_`，
    好跟字段名对齐。
    """
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except FileNotFoundError as exc:
        raise ConfigError(f"配置文件不存在: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"配置文件不是合法 TOML: {path}: {exc}") from exc
    return {str(key).replace("-", "_"): value for key, value in data.items()}
