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
from ..runtime.host_factory import create_host as default_host_factory
from ..terminal.session import TerminalSession
from .base import Session

_logger = get_logger("core.session.registry")

DEFAULT_JOURNAL_BUDGET = 8 << 20


@dataclass(frozen=True, slots=True)
class SessionKind:
    """一种会话形态：会话类 + 该形态的宿主工厂。

    `host_factory` 留空表示用注册表的默认工厂（`core.runtime` 的真宿主）。
    这是扩展点：接入方可以给某个标签配自己的宿主实现（如测试替身）。
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
        host_factory: HostFactory = default_host_factory,
        *,
        kinds: Mapping[str, SessionKind] | None = None,
        journal_budget_bytes: int = DEFAULT_JOURNAL_BUDGET,
    ) -> None:
        self._default_host_factory = host_factory
        self._kinds = dict(kinds or DEFAULT_KINDS)
        self._budget = journal_budget_bytes
        self._sessions: dict[str, Session] = {}

    def create(self, spec: SessionSpec) -> Session:
        """创建会话并启动它。

        **启动成功才入表**：宿主建不起来时异常向上抛，表里不会留下已关死的残骸
        （表 = 活着的会话的集合）。
        """
        kind = self._kinds.get(spec.mode)
        if kind is None:
            raise CoreError(f"未知会话模式: {spec.mode!r}")
        session = kind.session_class(
            new_uid(),
            spec,
            kind.host_factory or self._default_host_factory,
            journal_budget_bytes=self._budget,
        )
        session.start()
        self._sessions[session.uid] = session
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

    def detach(self, uid: str) -> Session:
        """把会话从注册表摘除，但**不做任何释放**。

        供上层做"两阶段释放"（见架构设计 §11）：先同步摘除——会话立刻从列表消失、
        不再被轮询、不再扇出——再把耗时的关闭交给别的线程。宿主关闭在部分平台上会
        长时间阻塞，压在事件循环上会冻住所有会话。
        """
        session = self._sessions.pop(uid, None)
        if session is None:
            raise SessionNotFound(uid)
        return session

    def close(self, uid: str) -> None:
        session = self.detach(uid)
        session.close()
        _logger.info("会话已从注册表移除 uid=%s", uid)

    def close_all(self) -> None:
        """关闭全部会话；单个失败不挡其余的，也不会留下半清空的表。"""
        for session in list(self._sessions.values()):
            try:
                session.close()
            except Exception as exc:  # 一个会话收尾失败不该挡别的
                _logger.warning("关闭会话异常 uid=%s: %s", session.uid, exc)
        self._sessions.clear()
