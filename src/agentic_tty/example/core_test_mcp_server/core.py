"""本进程里的核心层：装一个 `Runtime`，五个操作都是**对 core 的接口调用**。

没有连接、没有协议、没有子进程——会话与驱动都在本进程里。所有调用都发生在**事件循环
线程**上（工具是 `async` 的，SDK 直接在循环上 await 它们，而同步工具会被丢进线程池），
于是那个线程天然就是 core 要的"所有者线程"；`pump_forever()` 也挂在同一个循环上，会话的
输出因此一直在流。

会话快照（`_ref`）的字段与守护进程那台**逐字对齐**——那边是 `SessionRef`，两个台子换着用
时形状一致。
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

from ...core.errors import CoreError
from ...core.ports import SessionSpec
from ...core.runtime.input_queue import InputVerdict
from ...core.runtime.runtime import Runtime
from ...core.runtime.shell import default_shell
from ...core.session.base import Session
from ...core.session.state import SessionState
from ...core.terminal.session import TerminalSession

_PUMP_INTERVAL = 0.02
"""推进节拍，秒。一轮 `pump_all()` 的代价随会话数线性涨，台子上就几个会话。"""


class Core:
    """本进程里的那个核心层。"""

    def __init__(self) -> None:
        self._runtime = Runtime()
        self._closing = False

    # ── 五个操作（都由工具在事件循环线程上调用）─────────────────

    def create(
        self, mode: str, argv: Sequence[str], *, cwd: str | None, cols: int, rows: int
    ) -> dict[str, Any]:
        """起一个会话（程序留空 = 平台默认 shell），返回它的快照。"""
        spec = SessionSpec(
            mode=mode,
            argv=tuple(argv) or default_shell(),
            cols=cols,
            rows=rows,
            cwd=cwd,
        )
        return _ref(self._runtime.create(spec))

    def list(self) -> list[dict[str, Any]]:
        return [_ref(session) for session in self._runtime.list()]

    def read(self, uid: str) -> str:
        """会话当前可见屏幕的文本。"""
        session = self._runtime.get(uid)
        if not isinstance(session, TerminalSession):
            raise CoreError(f"{session.mode} 会话没有屏幕")
        return session.screen_text()

    def write(self, uid: str, data: bytes) -> bool:
        """往会话里写字节；返回**收下了没有**（整块超过输入队列硬上限时 `REJECTED`）。"""
        return self._runtime.send_input(uid, data) is not InputVerdict.REJECTED

    def close(self, uid: str) -> None:
        self._runtime.close(uid)

    # ── 推进与收尾 ─────────────────────────────────────────────

    def pump(self) -> None:
        """推进所有会话一轮——宿主读进来的字节靠它进会话。"""
        self._runtime.pump_all()

    async def pump_forever(self) -> None:
        """按节拍一直推进（由服务端的 lifespan 挂在事件循环上）。"""
        while not self._closing:
            self.pump()
            await asyncio.sleep(_PUMP_INTERVAL)

    def close_all(self) -> None:
        self._closing = True
        self._runtime.close_all()


def _ref(session: Session) -> dict[str, Any]:
    """会话快照——字段与守护进程那台的 `SessionRef` 一致。"""
    terminal = session if isinstance(session, TerminalSession) else None
    return {
        "uid": session.uid,
        "command": session.spec.argv[0] if session.spec.argv else "",
        "mode": session.mode,
        "state": str(session.state),
        "running": session.state is SessionState.RUNNING,
        "drained": session.drained,
        "exit_code": session.exit_code,
        "cols": terminal.cols if terminal is not None else None,
        "rows": terminal.rows if terminal is not None else None,
        "members": _members(session),
    }


def _members(session: Session) -> int | None:
    """进程树成员数；观测不到（未启动 / 没有作业对象 / 已关闭）返回 None。"""
    try:
        return len(session.descendants())
    except Exception:  # CoreError / MonitorUnavailable
        return None
