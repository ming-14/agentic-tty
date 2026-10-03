"""配置层：**本机实例的命名** ＋ **配置值 → 各层配置对象**。

- `names` —— 同一个实例在操作系统里叫什么（运行时目录 / 端点名 / 锁名）。两端各算一次，
  必然一致，所以不需要谁来发布状态。
- `sources` / `resolve` / `values` —— 值的来源（命令行 > 环境变量 > 文件 > 默认值）、合并
  规则，以及"值表 → 配置对象"的校验与类型转换。
- `daemon` / `consumer` —— **各层的配置对象就住在这个包里**，各层 import 它，不再各造一个
  `config.py`。

**它只压在 `foundation` 上**，所以 `transport` 够不着它（`transport` 与 `protocol` 彼此
独立）——端点名由调用方给**完整名字**，`transport` 不再自带前缀。
"""

from __future__ import annotations

from .consumer import ConsumerConfig
from .daemon import DaemonConfig
from .errors import ConfigError
from .names import DEFAULT_INSTANCE, PREFIX, endpoint_name, lock_name, runtime_dir
from .resolve import resolve
from .sources import ENV_PREFIX, env_values, file_values
from .values import defaults_of, from_values

__all__ = [
    "DEFAULT_INSTANCE",
    "ENV_PREFIX",
    "PREFIX",
    "ConfigError",
    "ConsumerConfig",
    "DaemonConfig",
    "defaults_of",
    "endpoint_name",
    "env_values",
    "file_values",
    "from_values",
    "lock_name",
    "resolve",
    "runtime_dir",
]
