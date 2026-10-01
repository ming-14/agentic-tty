"""标识与时间戳生成。"""

from __future__ import annotations

import itertools
import time
import uuid
from datetime import UTC, datetime

_counter = itertools.count(1)


def new_uid() -> str:
    """会话主键：uuid4，全局唯一且不变。"""
    return str(uuid.uuid4())


def new_message_id() -> str:
    """消息 id：毫秒时间 + 进程内自增，保证同进程内唯一，用于请求/响应关联。"""
    return f"{int(time.time() * 1000)}{next(_counter)}"


def now_timestamp() -> str:
    """本地时区、毫秒精度的 ISO 8601 时间戳（防重放窗口的时间源）。"""
    dt = datetime.now(UTC).astimezone()
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}"
