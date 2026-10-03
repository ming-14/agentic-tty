"""配置层：**本机实例的命名** ＋ **配置值的加载 / 合并**。

它只装"机制"，**不装任何一层的字段**——这是它和"横切磁铁"的分水岭：

- `names` —— 同一个实例在操作系统里叫什么（运行时目录 / 端点名 / 锁名）。两端各算一次，
  必然一致，所以不需要谁来发布状态。
- `sources` / `resolve` —— 值的四个来源（命令行 > 环境变量 > 文件 > 默认值）与合并规则。
- 各层的配置对象（`DaemonConfig` …）**留在各层**，只跟这里要两样：我该叫什么名字、
  我这组字段从哪来。

**它只压在 `foundation` 上**，所以 `transport` 够不着它（`transport` 与 `protocol` 彼此
独立）——端点名由调用方给**完整名字**，`transport` 不再自带前缀。
"""

from __future__ import annotations

from .errors import ConfigError
from .names import PREFIX, endpoint_name, lock_name, runtime_dir
from .resolve import ENV_PREFIX, resolve
from .sources import env_values, file_values

__all__ = [
    "ENV_PREFIX",
    "PREFIX",
    "ConfigError",
    "endpoint_name",
    "env_values",
    "file_values",
    "lock_name",
    "resolve",
    "runtime_dir",
]
