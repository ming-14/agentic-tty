"""平台默认 shell。

"默认起哪个 shell" 是平台知识，只出现在宿主实现里：上层不该写 `if windows` 分支。

它没进 `foundation`（那里放的是 `paths.py` 这类被 ≥2 层用的东西）——只有起 shell 会话的
那一处要用它，够不上"共享"的门槛。
"""

from __future__ import annotations

import os
import sys


def default_shell() -> tuple[str, ...]:
    """当前平台的默认交互式 shell，归一成 argv 列表。

    Windows 用 `COMSPEC`（命令行解释器的权威来源），其余平台用 `SHELL`，
    取不到就退到该平台的常规路径。
    """
    if sys.platform == "win32":
        return (os.environ.get("COMSPEC") or "cmd.exe",)
    return (os.environ.get("SHELL") or "/bin/sh",)
