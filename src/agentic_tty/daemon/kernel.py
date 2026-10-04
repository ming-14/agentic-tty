"""守护进程的**默认请求处理层**：uid 级请求 → core 操作。

它把 `daemon_ipc` 的命令翻成 core 调用——这就是守护进程对外的能力面。

接缝上的请求对象是 `WireRequest`，答复必须原样把**同一个对象**带回去——路由靠它。
"""

from __future__ import annotations

import os
import time
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, TypeVar, cast

from ..core.errors import CoreError, OffsetTrimmed
from ..core.ports import PTY, SUBPROCESS, SessionSpec, Stream
from ..core.process.session import ProcessSession
from ..core.runtime.bridge import Wakeup
from ..core.runtime.runtime import Runtime
from ..core.runtime.shell import default_shell
from ..core.session.base import Session
from ..core.session.registry import SessionKind, SessionRegistry
from ..core.session.state import SessionState
from ..core.session.subscription import Subscription
from ..core.terminal.session import TerminalSession
from ..foundation.errors import AgenticTtyError
from ..foundation.ids import now_timestamp
from ..foundation.logs import get_logger
from ..protocol.contracts.daemon_ipc import Command, Event, ReadMode, SessionRef, stream_tag
from ..protocol.envelope import Envelope
from ..protocol.errors import MessageError
from ..protocol.response import failed_response, ok_response
from .access_point import ByteChunk, WireRequest
from .handler import Delivery, Reply, StopSignal

_logger = get_logger("daemon.kernel")

_PUSH_BUDGET = 1 << 16
"""每轮每个订阅最多取多少字节——分片拉取，别把积压一次倒完。"""
_PUSH_INFLIGHT = 8
"""每个订阅最多允许多少帧在途（尚未被投递确认）——出站队列有限，别把它撑满。"""

_E = TypeVar("_E", bound=StrEnum)
"""取参辅助用的枚举类型。"""


@dataclass
class _Sub:
    """一条订阅：游标 ＋ 在途的推送帧。

    在途帧存下来是因为**取出来的字节不能丢**：`pull()` 一调，游标就往前走了，若这一帧
    没被收下（连接堵住），只能原样重发。
    """

    request: WireRequest
    session: Session
    stream: Stream
    cursor: Subscription
    out: deque[Envelope | ByteChunk] = field(default_factory=deque)
    done: bool = False
    """会话已排空：`out` 交完就注销。"""


def _text(op: Mapping[str, Any], key: str, default: str = "") -> str:
    value = op.get(key)
    return default if value is None else str(value)


def _number(op: Mapping[str, Any], key: str) -> int:
    """取一个整数参数；缺省为 0。

    对端给的可能是任何 JSON 值，`int()` 对"字符串但不是数"抛 `ValueError`、对"列表/对象"
    抛 `TypeError`——两者都是**对端送错东西**，属可预期结果，一律收成 `MessageError`
    （与 `protocol` 解消息体字段同源）。裸 `TypeError` 逃出去会被上层当成守护进程内部故障。
    """
    value = op.get(key)
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise MessageError(f"{key} 不是整数: {value!r}") from exc


def _real(op: Mapping[str, Any], key: str, default: float) -> float:
    """取一个实数参数（同 `_number`，只是出浮点）。"""
    value = op.get(key)
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise MessageError(f"{key} 不是数: {value!r}") from exc


def _enum(op: Mapping[str, Any], key: str, cls: type[_E], default: str = "") -> _E:
    """取一个枚举参数；不认识的值是**对端送错了**，收成 `MessageError`。"""
    raw = _text(op, key, default)
    try:
        return cls(raw)
    except ValueError as exc:
        raise MessageError(f"未知 {key}: {raw!r}") from exc


def _members(session: Session) -> int | None:
    """进程树成员数；观测不到（未启动 / 没有作业对象 / 已关闭）返回 None。"""
    try:
        return len(session.descendants())
    except Exception:  # CoreError / MonitorUnavailable
        return None


def _failed(envelope: Envelope, error: BaseException) -> Envelope:
    return failed_response(envelope.type, envelope.mid, type(error).__name__, str(error))


class KernelHandler:
    """默认的 `RequestHandler`：uid 级请求 → core 操作。"""

    def __init__(self, registry: SessionRegistry | None = None, *, endpoint: str = "") -> None:
        # 读线程读到数据就投一个信号，所有者循环靠它阻塞等待，不必定时轮询所有会话。
        self._wakeup = Wakeup()
        self._runtime = Runtime(
            registry
            or SessionRegistry(
                kinds={PTY: SessionKind(TerminalSession), SUBPROCESS: SessionKind(ProcessSession)}
            ),
            wakeup=self._wakeup,
        )
        self._endpoint = endpoint
        self._started_at = now_timestamp()
        self._started_monotonic = time.monotonic()
        self._subs: dict[str, _Sub] = {}
        """订阅表：**订阅 id = 那条 `subscribe` 请求的 `mid`**（连接内唯一）。"""
        self._stop: StopSignal | None = None
        """停机通道，由守护进程在装配时注入（见 `bind`）。"""

    # ════════════════════════════════════════════════════════════
    # RequestHandler（只在所有者线程上被调用，因此不需要锁）
    # ════════════════════════════════════════════════════════════

    def bind(self, stop: StopSignal) -> None:
        self._stop = stop

    def handle(self, request: object) -> Reply | None:
        """处理一条请求，**任何异常都收成失败答复**——不许穿透到守护进程。

        分两档（这是接缝的契约：`handle` 不抛异常）：

        - **可预期的失败**（`AgenticTtyError`：core 的领域错误、协议的对端错误、系统 IO）：
          对端送了坏东西或环境不配合，转成失败答复即可，**不记堆栈**——它不是内部缺陷。
        - **意外失败**（其余异常）：真出 bug 了，记 `logger.exception` 留堆栈，照样答复。
          不能让消费者干等，也不能让 `daemon` 的循环把一次请求失败当成进程故障。
        """
        wire = cast(WireRequest, request)
        try:
            answer = self._dispatch(wire)
        except AgenticTtyError as exc:
            return Reply(request=wire, answer=_failed(wire.envelope, exc))
        except Exception as exc:
            _logger.exception("处理请求时出现意外错误 mid=%s", wire.envelope.mid)
            return Reply(request=wire, answer=_failed(wire.envelope, exc))
        return Reply(request=wire, answer=answer)

    def poll(self) -> list[Reply]:
        """交出本轮可以推的帧——每个订阅最多一帧，且在途数受上限约束。"""
        replies: list[Reply] = []
        for sub_id, sub in list(self._subs.items()):
            reply = self._push(sub_id, sub)
            if reply is not None:
                replies.append(reply)
        return replies

    def pending(self) -> int:
        """压着等的请求数——**订阅不算**：它是长期挂着的，算进来会让收尾白等满 `drain_timeout`。"""
        return 0

    def on_reply(self, request: object, delivery: Delivery) -> None:
        """投递结果：收下了就把在途那帧划掉；连接没了就把这条订阅丢掉。

        没收下（`CONGESTED`）什么都不做——在途帧还留着，下一轮原样重发，游标不动。
        """
        wire = cast(WireRequest, request)
        sub = self._subs.get(wire.envelope.mid)
        if sub is None:
            return
        if delivery is Delivery.SENT:
            # 交付的一定是队首那一帧（`poll` 只交 `out[0]`）。subscribe 的 ack 走 `handle`
            # 直接返回、那时 `out` 还空着，所以这里不会把推送帧误划掉。
            if sub.out:
                sub.out.popleft()
        elif delivery is Delivery.GONE:
            self._subs.pop(wire.envelope.mid, None)

    def on_disconnected(self, connection: object) -> None:
        """连接没了：把挂在它上面的订阅全部注销。"""
        stale = [sid for sid, sub in self._subs.items() if sub.request.connection is connection]
        for sub_id in stale:
            self._subs.pop(sub_id, None)

    def failure(self, request: object, error: BaseException) -> Reply:
        wire = cast(WireRequest, request)
        return Reply(request=wire, answer=_failed(wire.envelope, error))

    def on_input(self, key: str, data: bytes) -> None:
        """字节帧上行 = 往那个会话写字节——**键就是 uid**。"""
        try:
            self._runtime.send_input(key, data)
        except CoreError as exc:  # 会话刚被关掉
            _logger.warning("上行字节无人接收 uid=%s: %s", key, exc)

    def pump(self) -> None:
        self._runtime.pump_all()

    def wait(self, timeout: float) -> None:
        """等会话侧的活（读线程经 `Wakeup` 投的信号），或超时。

        醒来后把积压的信号一并清掉：一轮就能把所有会话的桥排空，剩下的信号再叫醒只是白跑
        一遍，而且读线程**每读到一个块就投一个**，不清就会越积越多。
        """
        self._wakeup.wait(timeout)
        while self._wakeup.pending:
            self._wakeup.wait(0)

    def shutdown(self) -> None:
        self._subs.clear()
        self._runtime.close_all()

    # ════════════════════════════════════════════════════════════
    # 命令
    # ════════════════════════════════════════════════════════════

    def _dispatch(self, wire: WireRequest) -> Envelope | ByteChunk:
        envelope = wire.envelope
        command = envelope.type
        op = envelope.payload.op
        if command == Command.DAEMON_STATUS:
            return self._status(envelope)
        if command == Command.SHUTDOWN_DAEMON:
            return self._shutdown_daemon(envelope)
        if command == Command.CREATE_SESSION:
            return self._create(envelope, op)
        if command == Command.CLOSE_SESSION:
            self._runtime.close(_text(op, "uid"))
            return ok_response(command, envelope.mid, {})
        if command == Command.LIST_SESSIONS:
            refs = [self._ref(session).to_dict() for session in self._runtime.list()]
            return ok_response(command, envelope.mid, {"sessions": refs})
        if command == Command.READ_SESSION:
            return self._read(envelope, op)
        if command == Command.RESIZE_SESSION:
            return self._resize(envelope, op)
        if command == Command.SUBSCRIBE:
            return self._subscribe(wire)
        if command == Command.UNSUBSCRIBE:
            self._subs.pop(_text(op, "sub_id"), None)
            return ok_response(command, envelope.mid, {})
        raise MessageError(f"未知命令: {command}")

    # ════════════════════════════════════════════════════════════
    # 订阅
    # ════════════════════════════════════════════════════════════

    def _subscribe(self, wire: WireRequest) -> Envelope:
        """登记一条订阅：**订阅 id 就是这条请求的 `mid`**，推送按它分派回同一条连接。"""
        envelope = wire.envelope
        op = envelope.payload.op
        session = self._runtime.get(_text(op, "uid"))
        stream = _enum(op, "stream", Stream, Stream.STDOUT.value)
        cursor = op.get("cursor")
        sub = Subscription(session, stream, None if cursor is None else _number(op, "cursor"))
        self._subs[envelope.mid] = _Sub(wire, session, stream, sub)
        return ok_response(
            envelope.type,
            envelope.mid,
            {"sub_id": envelope.mid, "offset": sub.next_offset, "lossy": sub.lossy},
        )

    def _push(self, sub_id: str, sub: _Sub) -> Reply | None:
        while len(sub.out) < _PUSH_INFLIGHT and not sub.done:
            if not self._fill(sub_id, sub):
                break
        if not sub.out:
            if sub.done:
                self._subs.pop(sub_id, None)
            return None
        return Reply(sub.request, sub.out[0])

    def _fill(self, sub_id: str, sub: _Sub) -> bool:
        """按游标再取一帧放进 `out`；没有新东西返回 False。"""
        try:
            pull = sub.cursor.pull(_PUSH_BUDGET)
        except OffsetTrimmed:
            # 客户端太慢，游标被日志裁掉了：换个新游标重同步，它自带一份快照
            sub.cursor = Subscription(sub.session, sub.stream)
            sub.out.append(ok_response(Event.RESYNC, sub_id, {"lossy": sub.cursor.lossy}))
            return True
        added = False
        tag = stream_tag(sub.stream.value)
        # 尺寸变更可能落在这一段字节**中间**：按它把字节切开，发出去就是
        # 「旧尺寸的字节 / resize 帧 / 新尺寸的字节」——订阅者不必自己对齐 offset。
        cut = 0
        for event in pull.resizes:
            at = event.offset - pull.start
            if at > cut:
                sub.out.append(ByteChunk(tag, sub_id, pull.data[cut:at]))
            sub.out.append(
                ok_response(
                    Event.RESIZE,
                    sub_id,
                    {"offset": event.offset, "cols": event.cols, "rows": event.rows},
                )
            )
            cut = at
            added = True
        if cut < len(pull.data):
            sub.out.append(ByteChunk(tag, sub_id, pull.data[cut:]))
            added = True
        if not pull.data and sub.session.drained:
            sub.out.append(ok_response(Event.ENDED, sub_id, {"exit_code": sub.session.exit_code}))
            sub.done = True
            added = True
        return added

    def _shutdown_daemon(self, envelope: Envelope) -> Envelope:
        """置停机标志，让守护进程本轮循环结束后收尾。

        **先答复、再置标志**：返回值由调用方立刻投递（接入点上是同步进该连接的出站队列），
        所以客户端拿得到这条 ack；标志一置，循环下一轮就进 `_shutdown`，连接随之关闭。

        没绑通道（进程内嵌入、单测直接构造本层）时只记一条日志——不值得为它抛异常。
        """
        if self._stop is None:
            _logger.warning("没有停机通道，忽略 shutdown_daemon")
            return ok_response(envelope.type, envelope.mid, {"stopping": False})
        answer = ok_response(envelope.type, envelope.mid, {"stopping": True})
        self._stop.request_stop()
        return answer

    def _status(self, envelope: Envelope) -> Envelope:
        return ok_response(
            envelope.type,
            envelope.mid,
            {
                "pid": os.getpid(),
                "started_at": self._started_at,
                "uptime": round(time.monotonic() - self._started_monotonic, 3),
                "sessions": len(self._runtime.list()),
                "endpoint": self._endpoint,
            },
        )

    def _create(self, envelope: Envelope, op: Mapping[str, Any]) -> Envelope:
        argv = tuple(str(item) for item in (op.get("argv") or ())) or default_shell()
        spec = SessionSpec(
            mode=_text(op, "mode"),
            argv=argv,
            cols=_number(op, "cols") or 80,
            rows=_number(op, "rows") or 24,
            # 请求里给了就在那儿跑；没给 = 继承守护进程的目录（守护进程自己的目录由入口的
            # `--cwd` 定，见 `daemon/__main__.py`）
            cwd=_text(op, "cwd") or None,
        )
        # 会话与驱动一起建、一起起；起不来时 Runtime 自己把会话收掉，这里不留半个。
        session = self._runtime.create(spec)
        return ok_response(envelope.type, envelope.mid, {"session": self._ref(session).to_dict()})

    def _read(self, envelope: Envelope, op: Mapping[str, Any]) -> Envelope | ByteChunk:
        session = self._runtime.get(_text(op, "uid"))
        mode = _enum(op, "mode", ReadMode)
        stream = _enum(op, "stream", Stream, Stream.STDOUT.value)
        if mode is ReadMode.BYTES:
            # 只取尾部——`read_all` 会把整个保留区复制一遍，客户端每轮都读它。
            journal = session.journal_for(stream)
            tail = _number(op, "tail")
            start = max(journal.start_offset, journal.end_offset - tail) if tail > 0 else 0
            data = session.read_range(start, journal.end_offset, stream)
            return ByteChunk(tag=stream_tag(stream.value), key=envelope.mid, data=data)
        terminal = self._terminal(session)
        if mode is ReadMode.SCREEN:
            text = terminal.screen_text()
        elif mode is ReadMode.TEXT:
            text = terminal.full_text()
        elif mode is ReadMode.SVG:
            text = terminal.render_svg()
        else:  # IMAGE：位图由守护进程渲染好，客户端只管显示
            scale = _real(op, "scale", 1.0)
            return ByteChunk(
                tag=stream_tag(Stream.STDOUT.value),
                key=envelope.mid,
                data=terminal.render_image(scale=scale, fmt="png"),
            )
        # 顺带带上日志末尾的 offset：客户端靠它判断屏幕变了没有，变了才重渲染。
        return ok_response(
            envelope.type,
            envelope.mid,
            {"text": text, "offset": session.journal_for(Stream.STDOUT).end_offset},
        )

    def _resize(self, envelope: Envelope, op: Mapping[str, Any]) -> Envelope:
        cols = _number(op, "cols")
        rows = _number(op, "rows")
        if cols <= 0 or rows <= 0:
            raise MessageError(f"尺寸不合法: {cols}×{rows}")
        self._terminal(self._runtime.get(_text(op, "uid"))).resize(cols, rows)
        return ok_response(envelope.type, envelope.mid, {"cols": cols, "rows": rows})

    # ════════════════════════════════════════════════════════════
    # 内部
    # ════════════════════════════════════════════════════════════

    def _terminal(self, session: Session) -> TerminalSession:
        if not isinstance(session, TerminalSession):
            raise CoreError(f"{session.mode} 会话没有屏幕")
        return session

    @staticmethod
    def _ref(session: Session) -> SessionRef:
        terminal = session if isinstance(session, TerminalSession) else None
        return SessionRef(
            uid=session.uid,
            command=session.spec.argv[0] if session.spec.argv else "",
            mode=session.mode,
            state=str(session.state),
            running=session.state is SessionState.RUNNING,
            drained=session.drained,
            exit_code=session.exit_code,
            cols=terminal.cols if terminal is not None else None,
            rows=terminal.rows if terminal is not None else None,
            members=_members(session),
        )
