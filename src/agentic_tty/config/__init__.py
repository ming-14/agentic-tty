"""配置层：**契约公共层**——两端必须一致的那些**配置常量**。

- `constants` —— 名字前缀 / 默认实例名，以及由它们派生的**本机实例命名**（运行时目录 /
  端点名 / 锁名）。**命名只此一处**，两端各算一次必然一致。
- `daemon` / `consumer` —— 各层的配置对象：它们的字段默认值**就是配置常量**（写死在代码
  里），随装配注入，**不从任何地方加载**。

**它只压在 `foundation` 上**，所以 `transport` 够不着它（`transport` 与 `protocol` 彼此
独立）——端点名由调用方给**完整名字**，`transport` 不再自带前缀。
"""

from __future__ import annotations

from .constants import DEFAULT_INSTANCE, PREFIX, endpoint_name, lock_name, runtime_dir
from .consumer import ConsumerConfig
from .daemon import DaemonConfig

__all__ = [
    "DEFAULT_INSTANCE",
    "PREFIX",
    "ConsumerConfig",
    "DaemonConfig",
    "endpoint_name",
    "lock_name",
    "runtime_dir",
]
