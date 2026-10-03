"""本机对象的命名：同一个实例，在操作系统里叫什么。

**只认名字与目录，不认识任何一层**——所以守护进程与消费者各算一次，结果必然一样，
两端因此不需要任何"发现"协议，也不需要谁发布状态。

名字前缀**只此一处**：`transport` 拿 `endpoint_name()` 的结果当 `pipe://` 的 netloc，
自己不再拼前缀（它够不着这一层）。
"""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile
from pathlib import Path

PREFIX = "agentic-tty-"
"""本机对象（管道 / 锁 / socket）的名字前缀。"""


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
