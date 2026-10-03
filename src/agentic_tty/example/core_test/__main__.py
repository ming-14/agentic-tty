"""示例层入口：Tk 管理台。

    python -m agentic_tty.example

管理台直接接核心层（起会话 / 看屏幕 / 发输入 / 观测进程树），驱动由 core.runtime 的
`SessionRunner` 代劳。界面细节见 `gui.py`。
"""

from __future__ import annotations

import sys

from ...foundation.logs import configure
from .gui import main

if __name__ == "__main__":
    configure()
    sys.exit(main())
