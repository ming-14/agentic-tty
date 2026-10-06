"""会话运行时：把「会话」与它的「驱动」成对管起来。

`SessionRegistry` 只管会话，不认识读 / 写线程；`SessionRunner` 只管一个会话的驱动。
两者必须**成对**——少了驱动，会话的输出没人读、退出没人推进；少了会话，驱动无处附着。
把这层配对留给消费者，消费者就得自己维护「uid → runner」并手写两阶段释放
（`example/core_test_console/gui.py` 就抄过一遍）。

**`sid ↔ uid` 不在这里**：那是消费者语义（见架构设计 §7），消费者自己持会话目录。

**释放分两阶段**（见架构设计 §11）：先**同步摘除**——会话立刻从列表消失、不再被轮询、
不再扇出——再把耗时的宿主关闭交给别的线程；宿主关闭在部分平台上会长时间阻塞。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

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

RunnerFactory = Callable[[Session], SessionRunner]
"""驱动的构造方式。给了就**接管全部参数**（含 `wakeup`），没给就用内置那份。"""


class Runtime:
    """会话 + 驱动的成对管理。

    **三个注入点**（缺省一律用内置的）：`registry=` 会话表、`wakeup=` 唤醒通道、
    `runner_factory=` 驱动构造。传了 `runner_factory` 之后 `wakeup=` 不再起作用——
    接管了构造就接管了全部，与 `SessionKind.host_factory` 是同一个规矩。
    """

    def __init__(
        self,
        registry: SessionRegistry | None = None,
        *,
        wakeup: Wakeup | None = None,
        runner_factory: RunnerFactory | None = None,
    ) -> None:
        self._registry = registry if registry is not None else SessionRegistry()
        self._runner_factory = runner_factory or (
            lambda session: SessionRunner(session, wakeup=wakeup)
        )
        self._runners: dict[str, SessionRunner] = {}
        self._releasing: list[threading.Thread] = []

    # ── 会话 ───────────────────────────────────────────────────

    def create(self, spec: SessionSpec) -> Session:
        """创建会话并起驱动；驱动起不来就把会话收掉，不留半个。"""
        session = self._registry.create(spec)
        try:
            runner = self._runner_factory(session)
            # 先入表再启动：`start()` 半途抛错时 `close()` 才找得到这个驱动去停，
            # 否则它已经起的读写线程没人收。
            self._runners[session.uid] = runner
            runner.start()
        except Exception:
            # 走两阶段释放：宿主关闭可能长时间阻塞，不能压在调用线程上。
            self.close(session.uid)
            raise
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

    def pump(self, uid: str) -> list[PumpEvent]:
        """推进**一个**会话的驱动，返回它本轮的事件。

        驱动方平时走这条：唤醒通道点名说哪个会话有活，就只推进那一个——**一轮的代价
        与会话数无关**。uid 不在表里（刚被摘掉）返回空，不算错。
        """
        runner = self._runners.get(uid)
        return [] if runner is None else runner.pump()

    def pump_all(self) -> dict[str, list[PumpEvent]]:
        """推进**所有**会话的驱动，返回本轮有事件的那些（uid → 事件清单）。

        **没有唤醒通道时只能靠它**（轮询模式）；要"一次拿全事件清单"的驱动方也用它。
        有唤醒通道、又不看事件的驱动方走 `pump(uid)`——这里一轮的代价随会话数线性涨。
        """
        events: dict[str, list[PumpEvent]] = {}
        for uid in list(self._runners):
            got = self.pump(uid)
            if got:
                events[uid] = got
        return events

    def refresh_all(self) -> None:
        """全表问一次退出——**只问退出，不排空桥**。

        退出检测的兜底：绝大多数退出会由读线程的 EOF 唤醒带出来（那时顺手就 `refresh`
        了），但"进程退了、输出却还开着"（POSIX 上孙进程握着 slave）不会有 EOF 唤醒，
        只能靠这一遍兜住。调用方按**慢节拍**调它，别每轮都来。
        """
        for runner in list(self._runners.values()):
            runner.session.refresh()

    # ── 释放（两阶段）─────────────────────────────────────────

    def close(self, uid: str) -> None:
        """摘除一个会话，并把释放交给别的线程（**不在调用线程上等宿主关闭**）。

        未知 uid 抛 `SessionNotFound`（与 `SessionRegistry.detach` 同口径）。
        """
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
