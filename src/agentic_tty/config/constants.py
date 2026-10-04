"""配置常量：**两端必须一致的那些名字**。

同一个实例在操作系统里叫什么，由这几个常量派生——两端各算一次必然一致，所以不需要
任何"发现"协议，也不需要谁发布状态。
"""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile
from pathlib import Path

PREFIX = "agentic-tty-"
"""本机对象（管道 / 锁 / socket）的名字前缀。"""

DEFAULT_INSTANCE = "default"
"""默认实例名——守护进程与消费者共用同一个。"""


def runtime_dir(instance: str) -> Path:
    """本机放运行时文件的目录（锁 / 端点 / 日志）——不保证已存在。

    "该放哪"是平台知识（Windows 的 `%LOCALAPPDATA%`、Linux 的 XDG 目录），不是领域语义。
    """
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    else:
        base = (
            os.environ.get("XDG_RUNTIME_DIR")
            or os.environ.get("XDG_STATE_HOME")
            or str(Path.home() / ".local" / "state")
        )
    return Path(base) / instance


def endpoint_name(instance: str) -> str:
    """接入点的**完整名字**。

    `transport` 直接拿它当 `pipe://` 的 netloc——那边不认识命名习惯，只认完整名字。
    """
    return f"{PREFIX}{instance}"


def lock_name(instance: str, directory: Path) -> str:
    """单实例锁的名字。

    POSIX 的锁文件**天然落在运行时目录里**（路径本身就是命名空间）；Windows 的互斥体名
    却是**全局**的，所以把目录的 sha256 前 16 位并进去——否则"同名不同目录"的两份配置
    会互撞，与 POSIX 行为不一致。
    """
    if sys.platform != "win32":
        return str(directory / f"{PREFIX}{instance}.lock")
    digest = hashlib.sha256(str(directory).encode("utf-8")).hexdigest()[:16]
    return f"Local\\{PREFIX}{instance}-{digest}"
