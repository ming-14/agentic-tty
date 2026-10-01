"""会话注册表：按 uid 索引，按形态选择会话实现。"""

from __future__ import annotations

from ...foundation.ids import new_uid
from ...foundation.logs import get_logger
from ..errors import SessionNotFound
from ..ports import HostFactory, SessionMode, SessionSpec
from .base import Session

_logger = get_logger("core.registry")

DEFAULT_JOURNAL_BUDGET = 8 << 20


class SessionRegistry:
    """会话的创建、查询与回收。"""

    def __init__(
        self,
        host_factory: HostFactory,
        *,
        journal_budget_bytes: int = DEFAULT_JOURNAL_BUDGET,
    ) -> None:
        self._host_factory = host_factory
        self._budget = journal_budget_bytes
        self._sessions: dict[str, Session] = {}

    def create(self, spec: SessionSpec) -> Session:
        """创建会话对象（未启动）。"""
        from ..process.session import ProcessSession
        from ..terminal.session import TerminalSession

        cls = TerminalSession if spec.mode is SessionMode.PTY else ProcessSession
        uid = new_uid()
        session = cls(uid, spec, self._host_factory, journal_budget_bytes=self._budget)
        self._sessions[uid] = session
        return session

    def get(self, uid: str) -> Session:
        try:
            return self._sessions[uid]
        except KeyError:
            raise SessionNotFound(uid) from None

    def find(self, uid: str) -> Session | None:
        return self._sessions.get(uid)

    def list(self) -> list[Session]:
        return list(self._sessions.values())

    def close(self, uid: str) -> None:
        session = self._sessions.pop(uid, None)
        if session is None:
            raise SessionNotFound(uid)
        session.close()
        _logger.info("会话已从注册表移除 uid=%s", uid)

    def close_all(self) -> None:
        for session in list(self._sessions.values()):
            session.close()
        self._sessions.clear()
