"""会话运行时：把「会话」与它的「驱动」成对管起来。

`SessionRegistry` 只管会话，不认识读 / 写线程；`SessionRunner` 只管一个会话的驱动。
两者必须**成对**——少了驱动，会话的输出没人读、退出没人推进；少了会话，驱动无处附着。
把这层配对留给消费者，消费者就得自己维护「uid → runner」并手写两阶段释放
（`example/core_test/gui.py` 与 `example/daemon_test/handler.py` 各抄了一遍）。

**`sid ↔ uid` 不在这里**：那是消费者语义（见架构设计 §7），消费者自己持会话目录。

**释放分两阶段**（见架构设计 §11）：先**同步摘除**——会话立刻从列表消失、不再被轮询、
不再扇出——再把耗时的宿主关闭交给别的线程；宿主关闭在部分平台上会长时间阻塞。
"""

from __future__ import annotations

import threading
import time

from ...foundation.logs import get_logger
from ..errors import CoreError
from ..ports import SessionSpec
from ..session.base import Session
from ..session.registry import SessionRegistry
from .bridge import Wakeup
from .input_queue import InputVerdict
from .runner import PumpEvent, SessionRunner

_logger = get_logger("core.runtime.runtime")

DEFAULT_RELEASE_TIMEOUT = 5.0


class Runtime:
    """会话 + 驱动的成对管理。"""

    def __init__(
        self,
        registry: SessionRegistry | None = None,
        *,
        wakeup: Wakeup | None = None,
    ) -> None:
        self._registry = registry if registry is not None else SessionRegistry()
        self._wakeup = wakeup
        self._runners: dict[str, SessionRunner] = {}
        self._releasing: list[threading.Thread] = []

    # ── 会话 ───────────────────────────────────────────────────

    def create(self, spec: SessionSpec) -> Session:
        """创建会话并起驱动；驱动起不来就把会话收掉，不留半个。"""
        session = self._registry.create(spec)
        try:
            runner = SessionRunner(session, wakeup=self._wakeup)
            runner.start()
        except Exception:
            # 走两阶段释放：宿主关闭可能长时间阻塞，不能压在调用线程上。
            self.close(session.uid)
            raise
        self._runners[session.uid] = runner
        return session

    def get(self, uid: str) -> Session:
        return self._registry.get(uid)

    def find(self, uid: str) -> Session | None:
        return self._registry.find(uid)

    def list(self) -> list[Session]:
        return self._registry.list()

    def runner(self, uid: str) -> SessionRunner | None:
        return self._runners.get(uid)

    def send_input(self, uid: str, data: bytes) -> InputVerdict:
        """把输入交给某个会话的写线程；会话没有驱动时抛 `CoreError`。"""
        runner = self._runners.get(uid)
        if runner is None:
            raise CoreError(f"会话没有驱动: {uid}")
        return runner.submit_input(data)

    def pump_all(self) -> dict[str, list[PumpEvent]]:
        """推进所有会话的驱动，返回本轮有事件的那些（uid → 事件清单）。"""
        events: dict[str, list[PumpEvent]] = {}
        for uid, runner in list(self._runners.items()):
            got = runner.pump()
            if got:
                events[uid] = got
        return events

    # ── 释放（两阶段）─────────────────────────────────────────

    def close(self, uid: str) -> None:
        """摘除一个会话，并把释放交给别的线程（**不在调用线程上等宿主关闭**）。"""
        self._release(self._registry.detach(uid), self._runners.pop(uid, None))

    def close_all(self, *, timeout: float = DEFAULT_RELEASE_TIMEOUT) -> None:
        """收尾全部会话：先全部同步摘除（列表立刻空），再统一等释放线程。"""
        for session in list(self._registry.list()):
            self._release(
                self._registry.detach(session.uid), self._runners.pop(session.uid, None)
            )
        self._join_releases(timeout)

    def _release(self, session: Session, runner: SessionRunner | None) -> None:
        self._releasing = [thread for thread in self._releasing if thread.is_alive()]
        thread = threading.Thread(
            target=self._release_now,
            args=(session, runner),
            name=f"release-{session.uid[:8]}",
            daemon=True,
        )
        thread.start()
        self._releasing.append(thread)

    @staticmethod
    def _release_now(session: Session, runner: SessionRunner | None) -> None:
        try:
            session.close()  # 先强杀进程树再关宿主，可能要等
        except Exception as exc:  # 收尾失败不该把异常丢在后台线程里
            _logger.warning("释放会话异常 uid=%s: %s", session.uid, exc)
        finally:
            if runner is not None:
                runner.stop()

    def _join_releases(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        for thread in self._releasing:
            thread.join(max(0.0, deadline - time.monotonic()))
        self._releasing.clear()
