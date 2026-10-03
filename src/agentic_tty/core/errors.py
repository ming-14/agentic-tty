"""核心层错误。"""

from __future__ import annotations

from ..foundation.errors import AgenticTtyError


class CoreError(AgenticTtyError):
    """核心层错误基类。"""


class SessionNotFound(CoreError):
    """按 uid 取会话时不存在。"""

    def __init__(self, uid: str) -> None:
        super().__init__(f"会话不存在: {uid}")


class SessionStateError(CoreError):
    """非法的生命周期迁移。"""


class OffsetAhead(CoreError):
    """客户端游标超前于日志末尾——协议不一致，不静默重同步。"""
