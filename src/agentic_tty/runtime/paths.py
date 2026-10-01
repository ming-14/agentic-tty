"""运行时目录：pid / 锁 / 端点文件放这里。

"该放哪"是平台知识（Windows 的 `%LOCALAPPDATA%`、Linux 的 XDG 目录），按分层规则
只能出现在运行时层。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path


def default_runtime_dir(name: str = "agentic-tty") -> Path:
    """本机放运行时文件的目录（不保证已存在）。"""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    else:
        base = (
            os.environ.get("XDG_RUNTIME_DIR")
            or os.environ.get("XDG_STATE_HOME")
            or str(Path.home() / ".local" / "state")
        )
    return Path(base) / name
