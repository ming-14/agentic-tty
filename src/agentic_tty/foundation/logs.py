"""日志基础设施。

统一命名空间（`agentic_tty.<name>`）与格式；`configure()` 幂等，进程内只装一次 handler。
"""

from __future__ import annotations

import logging
import sys

_ROOT = "agentic_tty"
_configured = False


def get_logger(name: str) -> logging.Logger:
    """取带命名空间的 logger。"""
    return logging.getLogger(f"{_ROOT}.{name}" if name else _ROOT)


def configure(level: int = logging.INFO) -> None:
    """装一次 stderr handler（幂等）。"""
    global _configured
    if _configured:
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger(_ROOT)
    root.addHandler(handler)
    root.setLevel(level)
    root.propagate = False
    _configured = True
