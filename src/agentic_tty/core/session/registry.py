"""会话注册表：按 uid 索引，按模式标签选择会话形态。

模式是**开放字符串**（见 `ports.SessionSpec.mode`）：注册表不写死 pty / subprocess，
而是查一份 `标签 → 会话形态` 的映射。**形态 = 会话类 + 宿主工厂**，两者必须成对
注册——拆成两份映射各自维护，就会出现"假宿主配真会话类"这类只在运行期才炸的组合。
缺省给内置两种形态，接入方可传入自己的映射。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from ...foundation.ids import new_uid
from ...foundation.logs import get_logger
from ..errors import CoreError, SessionNotFound
from ..ports import PTY, SUBPROCESS, HostFactory, SessionSpec
from ..process.session import ProcessSession
from ..terminal.session import TerminalSession
from .base import Session

_logger = get_logger("core.registry")

DEFAULT_JOURNAL_BUDGET = 8 << 20


@dataclass(frozen=True, slots=True)
class SessionKind:
    """一种会话形态：会话类 + 该形态的宿主工厂。

    `host_factory` 留空表示用注册表的默认工厂：内置两种形态的宿主都由装配层注入
    （运行时层的真 PTY / 真子进程），core 因此不必 import 任何宿主实现。
    """

    session_class: type[Session]
    host_factory: HostFactory | None = None


DEFAULT_KINDS: dict[str, SessionKind] = {
    PTY: SessionKind(TerminalSession),
    SUBPROCESS: SessionKind(ProcessSession),
}


class SessionRegistry:
    """会话的创建、查询与回收。"""

    def __init__(
        self,
        host_factory: HostFactory,
        *,
        kinds: Mapping[str, SessionKind] | None = None,
        journal_budget_bytes: int = DEFAULT_JOURNAL_BUDGET,
    ) -> None:
        self._default_host_factory = host_factory
        self._kinds = dict(kinds or DEFAULT_KINDS)
        self._budget = journal_budget_bytes
        self._sessions: dict[str, Session] = {}

    def create(self, spec: SessionSpec) -> Session:
        """创建会话对象（未启动）。"""
        kind = self._kinds.get(spec.mode)
        if kind is None:
            raise CoreError(f"未知会话模式: {spec.mode!r}")
        uid = new_uid()
        session = kind.session_class(
            uid,
            spec,
            kind.host_factory or self._default_host_factory,
            journal_budget_bytes=self._budget,
        )
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
