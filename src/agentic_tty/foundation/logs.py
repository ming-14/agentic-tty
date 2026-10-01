"""日志基础设施。

统一命名空间（`agentic_tty.<name>`）与格式；`configure()` 幂等，进程内只装一次 handler。
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

_ROOT = "agentic_tty"
_configured = False
_file_handlers: set[str] = set()

_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def get_logger(name: str) -> logging.Logger:
    """取带命名空间的 logger。"""
    return logging.getLogger(f"{_ROOT}.{name}" if name else _ROOT)


def configure(level: int = logging.INFO) -> None:
    """装一次 stderr handler（幂等）。"""
    global _configured
    if _configured:
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(_FORMAT))
    root = logging.getLogger(_ROOT)
    root.addHandler(handler)
    root.setLevel(level)
    root.propagate = False
    _configured = True


def add_rotating_file(path: Path, *, max_bytes: int = 4 << 20, backups: int = 3) -> None:
    """再加一个按大小轮转的文件 handler（同一路径只装一次）。

    常驻进程的日志不能只往 stderr 写——没有轮转就会长到把盘吃满。
    这里**不动 logger 的级别**：级别由 `configure()` 一处决定。
    """
    key = str(path)
    if key in _file_handlers:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(path, maxBytes=max_bytes, backupCount=backups, encoding="utf-8")
    handler.setFormatter(logging.Formatter(_FORMAT))
    logging.getLogger(_ROOT).addHandler(handler)
    _file_handlers.add(key)
