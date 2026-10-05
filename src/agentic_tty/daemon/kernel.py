"""守护进程的**默认请求处理层**：uid 级请求 → core 操作。

它把 `daemon_ipc` 的命令翻成 core 调用——这就是守护进程对外的能力面。

接缝上的请求对象是 `WireRequest`，答复必须原样把**同一个对象**带回去——路由靠它。
"""

from __future__ import annotations

import os
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, TypeVar, cast

from ..core.errors import CoreError, OffsetTrimmed
from ..core.ports import SessionSpec, Stream
from ..core.runtime.bridge import Wakeup
from ..core.runtime.input_queue import InputVerdict
from ..core.runtime.runtime import Runtime
from ..core.runtime.shell import default_shell
from ..core.session.base import Session
from ..core.session.registry import SessionRegistry
from ..core.session.state import SessionState
from ..core.session.subscription import Subscription
from ..core.terminal.session import TerminalSession
from ..foundation.errors import AgenticTtyError
from ..foundation.ids import now_timestamp
from ..foundation.logs import get_logger
from ..protocol.contracts.daemon_ipc import Command, Event, ReadMode, SessionRef, stream_tag
from ..protocol.envelope import Envelope, to_json
from ..protocol.errors import MessageError
from ..protocol.frame import encode_bytes, encode_control
from ..protocol.response import failed_response, ok_response
from .access_point import ByteChunk, WireRequest
from .handler import Delivery, InputAction, Reply, StopSignal

_logger = get_logger("daemon.kernel")

_PUSH_BUDGET = 1 << 16
"""每轮每个订阅最多取多少字节——分片拉取，别把积压一次倒完。"""
_PUSH_INFLIGHT = 8
"""每个订阅**一轮最多交几帧**，也就是最多允许多少帧在途（尚未被投递确认）。

它同时是两条约束：出站队列有限，别把它撑满；以及一轮别把整段积压倒完（额度缺失时
只有它在兜底）。8 帧 × `_PUSH_BUDGET` = 512 KiB，落在 1 MiB 出站队列之内。
"""

_E = TypeVar("_E", bound=StrEnum)
"""取参辅助用的枚举类型。"""

_INPUT_ACTIONS: dict[InputVerdict, InputAction] = {
    InputVerdict.QUEUED: InputAction.NONE,
    InputVerdict.HOLD: InputAction.HOLD,
    InputVerdict.REJECTED: InputAction.DROP,
}
"""core 的输入判定 → 让守护进程对那条连接做什么（见架构设计 §12）。"""

_VIEWS: dict[ReadMode, Callable[[TerminalSession], str]] = {
    ReadMode.SCREEN: TerminalSession.screen_text,
    ReadMode.TEXT: TerminalSession.full_text,
    ReadMode.SVG: TerminalSession.render_svg,
}
"""出文本的读视图模式 → 对应的方法。**`BYTES` 不在表里**——它出字节帧，在 `_read` 里提前返回。"""

_EXIT_SWEEP_INTERVAL = 1.0
"""全表问一次退出（兜底）的最短间隔，秒。

退出绝大多数由读线程的 EOF 唤醒带出来；这一遍只为"进程退了、输出却还开着"那种。节拍
只决定那种情形多久被发现一次，慢一点没关系——它本来就不 drained。
"""


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
    handed: int = 0
    """`out` 里已被本轮交出、等投递结果的**前缀**帧数（见 `poll` / `on_reply` 的约定）。"""


def _text(op: Mapping[str, Any], key: str, default: str = "") -> str:
    value = op.get(key)
    return default if value is None else str(value)


def _number(op: Mapping[str, Any], key: str) -> int:
    """取一个整数参数；缺省为 0。

    对端给的可能是任何 JSON 值，`int()` 对"字符串但不是数"抛 `ValueError`、对"列表/对象"
    抛 `TypeError`——两者都是**对端送错东西**，收成 `MessageError`（与 `protocol` 解消息体
    字段同源），客户端据此能看出是请求本身的毛病，而不是一个看不出所以然的 `TypeError`。
    """
    value = op.get(key)
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise MessageError(f"{key} 不是整数: {value!r}") from exc


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


def _frame_bytes(frame: Envelope | ByteChunk) -> int:
    """一帧在出站队列里占多少字节——**按编成后的实际大小算**。

    接入点按编好的字节记账，所以预算也得用同一个口径；拿负载长度估会偏小，
    额度就形同虚设（贴在边上交、每次都超）。
    """
    if isinstance(frame, ByteChunk):
        return len(encode_bytes(frame.tag, frame.key, frame.data))
    return len(encode_control(to_json(frame)))


def _size_fields(session: Session, offset: int) -> dict[str, int | None]:
    """答复里带上的尺寸字段：`offset` 处生效的基线（无屏幕会话为 `None`）。

    尺寸变更史可能已被日志裁剪裁短，只靠随后的 `resize` 帧推不出基线——订阅 ack 与
    重同步帧都得带上它，客户端才知道第一段字节按多大解释。
    """
    size = session.size_at(offset)
    return {"cols": size[0] if size else None, "rows": size[1] if size else None}


class KernelHandler:
    """默认的 `RequestHandler`：uid 级请求 → core 操作。"""

    def __init__(self, registry: SessionRegistry | None = None, *, endpoint: str = "") -> None:
        # 读线程读到数据就投一个信号（带 uid），所有者循环靠它阻塞等待并**只推进那一个**。
        self._wakeup = Wakeup()
        # 不传注册表就用 core 的默认形态表（pty / localpty / subprocess）——模式由 core 定义，
        # 守护进程不再自己另列一份，否则 core 加了模式这里会静默漏掉。
        self._runtime = Runtime(registry or SessionRegistry(), wakeup=self._wakeup)
        self._endpoint = endpoint
        self._started_at = now_timestamp()
        self._started_monotonic = time.monotonic()
        self._awake: str | None = None
        """`wait` 记下的那个 uid——`pump` 只推进它。"""
        self._last_exit_sweep = time.monotonic()
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

        - **可预期的失败**：对端送了坏东西，或环境不配合（命令不存在、目录没权限、
          管道断了）——转成失败答复即可，**不记堆栈**。它不是内部缺陷，打堆栈只会
          把真 bug 淹掉。含两类：`AgenticTtyError`（领域错误 / 对端错误；宿主侧就是
          `HostSpawnError` / `DependencyMissing`），以及 `OSError`——它是标准库的类型、
          纳不进本工程的错误体系，但语义同样是"操作没做成"。
        - **意外失败**（其余异常）：真出 bug 了，记 `logger.exception` 留堆栈，照样答复。
          不能让消费者干等，也不能让 `daemon` 的循环把一次请求失败当成进程故障。
        """
        wire = cast(WireRequest, request)
        try:
            answer = self._dispatch(wire)
        except (AgenticTtyError, OSError) as exc:
            return Reply(request=wire, answer=_failed(wire.envelope, exc))
        except Exception as exc:
            _logger.exception("处理请求时出现意外错误")
            return Reply(request=wire, answer=_failed(wire.envelope, exc))
        return Reply(request=wire, answer=answer)

    def poll(self, room_of: Callable[[object], int | None]) -> list[Reply]:
        """交出本轮可以推的帧——**在连接额度内尽量多交**，而不是每订阅一帧。

        一条订阅可以在一轮里连交几帧：额度（字节）用光、在途数到顶或队列排空才停。
        这样一段突发输出（`cat` 大文件、`make` 日志、位图重绘）几轮就能追上。

        交出去的帧**留在 `out` 里**（`handed` 标到哪了），等 `on_reply` 逐帧确认才划掉：
        没收下的那些自然还排着队，下一轮原样重发。
        """
        replies: list[Reply] = []
        for sub_id, sub in list(self._subs.items()):
            replies.extend(self._drain(sub_id, sub, room_of(sub.request)))
        return replies

    def _drain(self, sub_id: str, sub: _Sub, room: int | None) -> list[Reply]:
        """把一条订阅「本轮能交的」都交出去，返回它们（按顺序）。

        `room` 是这条连接的剩余字节额度（`None` = 不限额）。每交一帧扣掉它的字节数，
        额度放不下下一帧就停；再受 `_PUSH_INFLIGHT` 约束（在途数到顶，别把出站队列撑满）。
        """
        out: list[Reply] = []
        while sub.handed < _PUSH_INFLIGHT:
            frame = self._peek(sub_id, sub)
            if frame is None:
                return out
            size = _frame_bytes(frame)
            if room is not None and out and size > room:
                return out  # 本轮额度用光
            out.append(Reply(sub.request, frame))
            sub.handed += 1
            room = None if room is None else room - size
        return out

    def _peek(self, sub_id: str, sub: _Sub) -> Envelope | ByteChunk | None:
        """要交的第 `handed` 帧；`out` 里没有就补，补不出（且已排空）就把订阅摘掉。"""
        while len(sub.out) <= sub.handed and not sub.done:
            if not self._fill(sub_id, sub):
                break
        if len(sub.out) <= sub.handed:
            if sub.done and not sub.handed:
                self._subs.pop(sub_id, None)
            return None
        return sub.out[sub.handed]

    def pending(self) -> int:
        """压着等的请求数——**订阅不算**：它是长期挂着的，算进来会让收尾白等满 `drain_timeout`。"""
        return 0

    def on_reply(self, request: object, delivery: Delivery) -> None:
        """投递结果：收下了就把在途那帧划掉；连接没了就把这条订阅丢掉。

        确认是**逐帧**的（一轮可能交了好几帧，守护进程交一帧回告一次）：`SENT` 划掉
        **队首**那帧——批次是按顺序交的，回告也按顺序到，队首正好是对应的那个。

        `CONGESTED` 说明连队首都没被收下，于是 `handed` 归零：这一帧连同后面那些
        已经交出、还没确认的帧一起退回，下一轮**原样重发**，游标不动。
        """
        wire = cast(WireRequest, request)
        sub = self._subs.get(wire.envelope.mid)
        if sub is None:
            return
        if delivery is Delivery.SENT:
            # subscribe 的 ack 走 `handle` 直接返回、那时 `out` 还空着，所以这里不会误划。
            if sub.out and sub.handed:
                sub.out.popleft()
                sub.handed -= 1
        elif delivery is Delivery.CONGESTED:
            sub.handed = 0
        elif delivery is Delivery.GONE:
            self._subs.pop(wire.envelope.mid, None)

    def owns_retransmission(self, request: object) -> bool:
        """订阅推送的重发归本层（帧摆在 `out` 里，下一轮原样再交）；其余归守护进程。"""
        wire = cast(WireRequest, request)
        return wire.envelope.mid in self._subs

    def on_disconnected(self, connection: object) -> None:
        """连接没了：把挂在它上面的订阅全部注销。"""
        stale = [sid for sid, sub in self._subs.items() if sub.request.connection is connection]
        for sub_id in stale:
            self._subs.pop(sub_id, None)

    def failure(self, request: object, error: BaseException) -> Reply:
        wire = cast(WireRequest, request)
        return Reply(request=wire, answer=_failed(wire.envelope, error))

    def on_input(self, key: str, data: bytes, connection: object) -> InputAction:
        """字节帧上行 = 往那个会话写字节——**键就是 uid**；把 core 的判定翻成动作。

        队列越软水位就 `HOLD`（让发送方本端排队）、超硬上限就 `DROP`（整块没收下，违约）。
        会话刚被关掉时无处可写，记一笔即可——那不是对端的错，不该断它的连接。
        """
        try:
            verdict = self._runtime.send_input(key, data)
        except CoreError as exc:
            _logger.warning("上行字节无人接收 uid=%s: %s", key, exc)
            return InputAction.NONE
        return _INPUT_ACTIONS[verdict]

    def input_drained(self, key: str) -> bool:
        """那个会话的输入队列排空了没；会话已不在就是排空了（没什么可等）。"""
        runner = self._runtime.runner(key)
        return True if runner is None else runner.input_drained

    def pump(self) -> None:
        """推进**被唤醒的那一个**会话；再按慢节拍兜一遍退出检测。

        唤醒通道一个 uid 只留一份（见 `Wakeup`），所以这里不必像从前那样"取出来就扔"：
        uid 拿得住，也就能只推进它——**一轮的代价与会话数无关**。

        退出检测绝大多数由读线程的 EOF 唤醒带出来（那时顺手就 `refresh` 了）。兜底那遍
        是给"进程退了、输出却还开着"那种的，按慢节拍走，别每轮都全表问一遍。
        """
        awake, self._awake = self._awake, None
        if awake is not None:
            self._runtime.pump(awake)
        now = time.monotonic()
        if now - self._last_exit_sweep >= _EXIT_SWEEP_INTERVAL:
            self._last_exit_sweep = now
            self._runtime.refresh_all()

    def wait(self, timeout: float) -> None:
        """等会话侧的活（读线程经 `Wakeup` 投的信号），或超时；**记下是哪个会话**。

        只记不推进：推进要等 `pump`，那才是"轮到所有者线程干活"的地方。
        """
        self._awake = self._wakeup.wait(timeout)

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
            {
                "sub_id": envelope.mid,
                "offset": sub.next_offset,
                "lossy": sub.lossy,
                **_size_fields(session, sub.next_offset),
            },
        )

    def _fill(self, sub_id: str, sub: _Sub) -> bool:
        """按游标再取一帧放进 `out`；没有新东西返回 False。"""
        try:
            pull = sub.cursor.pull(_PUSH_BUDGET)
        except OffsetTrimmed:
            # 客户端太慢，游标被日志裁掉了：换个新游标重同步，它自带一份快照
            sub.cursor = Subscription(sub.session, sub.stream)
            payload = {"lossy": sub.cursor.lossy}
            payload.update(_size_fields(sub.session, sub.cursor.next_offset))
            sub.out.append(ok_response(Event.RESYNC, sub_id, payload))
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
        text = _VIEWS[mode](terminal)
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
