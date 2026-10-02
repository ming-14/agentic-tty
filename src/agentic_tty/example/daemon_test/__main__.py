"""守护进程验证台入口。

    python -m agentic_tty.example.daemon_test

Tk 的 mainloop 占主线程，守护进程的所有者循环在后台线程里跑——见 `gui.py`。
"""

from __future__ import annotations

import sys

from ...foundation.logs import configure
from .gui import main

if __name__ == "__main__":
    configure()
    sys.exit(main())
