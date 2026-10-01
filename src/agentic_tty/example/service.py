"""请求处理层的演示替身。

它顶替的是正式的 `service`：同样按 `daemon` 的 `RequestHandler` 协议实现，但只做演示
需要的那一小块——`sid` 目录、命令表、最小等待引擎。**没有订阅推送、没有插件、没有通知**。

正式 `service` 就位后只换装配入口里注入的那个对象，`daemon` 的机制一行不动。

所有方法都只在**所有者线程**（`Daemon.run` 所在的那个线程）上被调用，所以这里没有任何锁。
"""

from __future__ import annotations

import os
import shlex
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from ..core.errors import CoreError
from ..core.ports import PTY, SessionSpec, Stream
from ..core.session.base import Session
from ..core.session.registry import SessionRegistry
from ..core.terminal.session import TerminalSession
from ..daemon.handler import Reply
from ..foundation.errors import AgenticTtyError
from ..foundation.ids import now_timestamp
from ..foundation.logs import get_logger
from ..protocol.envelope import Envelope
from ..protocol.messages import (
    Command,
    Condition,
    DaemonStatus,
    Kind,
    ReadMode,
    SessionInfo,
    failed_response,
    ok_response,
)
from ..runtime.runner import SessionRunner
from ..runtime.shell import default_shell
from .sessions import create_registry

_logger = get_logger("example.service")

_DEFAULT_COLS = 80
_DEFAULT_ROWS = 24
_RELEASE_JOIN_SECONDS = 5.0
# 返回条件词汇的唯一来源是 protocol.Condition；按取值形态分两组（秒数 / 布尔）
_WAIT_DURATIONS = (Condition.IDLE, Condition.TIMEOUT)
_WAIT_FLAGS = (Condition.ENDED, Condition.CRASHED)


class ExampleServiceError(AgenticTtyError):
    """演示请求处理层的错误基类。错误类名会当作 `code` 发给客户端。"""


class BadRequest(ExampleServiceError):
    """请求参数缺失或类型不对。"""


class NoSuchSession(ExampleServiceError):
    """sid 不在目录里。"""


class SessionExisted(ExampleServiceError):
    """sid 已被占用。"""


# ════════════════════════════════════════════════════════════════════
# 返回条件
# ════════════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class WaitSpec:
    """一次等待声明了哪些条件；全为空表示立即返回。"""

    ended: bool = False
    crashed: bool = False
    idle: float | None = None
    timeout: float | None = None

    @property
    def is_immediate(self) -> bool:
        return not (self.ended or self.crashed or self.idle is not None or self.timeout is not None)

    @classmethod
    def parse(cls, raw: Mapping[str, Any]) -> WaitSpec:
        """解析线格式的 condition 组；未知条件直接报错，不静默忽略。"""
        unknown = sorted(set(raw) - {*_WAIT_DURATIONS, *_WAIT_FLAGS})
        if unknown:
            raise BadRequest(f"未知返回条件: {', '.join(unknown)}")
        for name in _WAIT_DURATIONS:
            value = raw.get(name)
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise BadRequest(f"返回条件 {name} 必须是正的秒数")
        for name in _WAIT_FLAGS:
            if name in raw and not isinstance(raw[name], bool):
                raise BadRequest(f"返回条件 {name} 必须是布尔值")
        return cls(
            ended=bool(raw.get(Condition.ENDED, False)),
            crashed=bool(raw.get(Condition.CRASHED, False)),
            idle=float(raw[Condition.IDLE]) if Condition.IDLE in raw else None,
            timeout=float(raw[Condition.TIMEOUT]) if Condition.TIMEOUT in raw else None,
        )


# ════════════════════════════════════════════════════════════════════
# 内部状态
# ════════════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class _Entry:
    sid: str
    uid: str
    tags: tuple[str, ...] = ()


@dataclass(slots=True)
class _Activity:
    """一个会话的"最近可观察变化"，idle 条件要看它。

    "什么算变化"按会话形态定：pty 看**可见文本**（满屏重绘的 TUI 字节在变、画面没变），
    子进程看**字节流**（它没有屏幕）。
    """

    at: float
    offsets: dict[Stream, int] = field(default_factory=dict)
    screen: str | None = None


@dataclass(frozen=True, slots=True)
class _Produced:
    """一个命令产出的返回数据。"""

    data: dict[str, Any]
    kind: Kind = Kind.TEXT
    stream: str = "stdout"
    binary: bytes | None = None


@dataclass(frozen=True, slots=True)
class _Command:
    produce: Callable[[Mapping[str, Any]], _Produced]
    waits: bool = False


@dataclass(slots=True)
class _Pending:
    """一个等待中的请求。

    连命令一起带上是刻意的：等它命中时不用再查一次命令表，也就不会出现"查不到"这种
    根本不该发生的分支。
    """

    request: Envelope
    entry: _Command
    sid: str
    spec: WaitSpec
    started: float


# ════════════════════════════════════════════════════════════════════


class ExampleService:
    """`RequestHandler` 的演示实现。"""

    def __init__(self, *, listen: Callable[[], str] | None = None) -> None:
        self._registry: SessionRegistry = create_registry()
        self._listen = listen or (lambda: "")
        self._entries: dict[str, _Entry] = {}
        self._runners: dict[str, SessionRunner] = {}
        self._activity: dict[str, _Activity] = {}
        self._pending: list[_Pending] = []
        self._releasing: list[threading.Thread] = []
        self._started_at = time.monotonic()
        self._started_stamp = now_timestamp()
        self._commands: dict[Command, _Command] = {
            Command.DAEMON_STATUS: _Command(self._cmd_status),
            Command.CREATE_SHELL_TERMINAL: _Command(self._cmd_create_shell_terminal),
            Command.CREATE_TERMINAL: _Command(self._cmd_create_terminal),
            Command.LIST_SESSIONS: _Command(self._cmd_list_sessions),
            Command.REMOVE_SESSION: _Command(self._cmd_remove_session),
            Command.INPUT_INTO_TERMINAL: _Command(self._cmd_input),
            Command.READ_TERMINAL: _Command(self._cmd_read, waits=True),
            Command.RESIZE_TERMINAL: _Command(self._cmd_resize),
        }

    # ════════════════════════════════════════════════════════════
    # RequestHandler
    # ════════════════════════════════════════════════════════════

    def handle(self, envelope: Envelope) -> Reply | None:
        """处理一条请求；返回 `None` 表示已登记等待，稍后由 `poll` 交出响应。"""
        entry = self._command(envelope)
        if isinstance(entry, Reply):
            return entry
        try:
            spec = WaitSpec.parse(envelope.payload.condition)
        except ExampleServiceError as exc:
            return self._fail(envelope, type(exc).__name__, str(exc))
        if spec.is_immediate:
            return self._reply(envelope, entry)
        if not entry.waits:
            return self._fail(envelope, "BadRequest", f"{envelope.type} 不支持返回条件")
        sid = envelope.payload.op.get("sid")
        if not isinstance(sid, str) or not sid:
            return self._fail(envelope, "BadRequest", "带返回条件的命令必须指定 sid")
        if sid not in self._entries:  # 会话不存在要立刻报，不能等到超时才说
            return self._fail(envelope, "NoSuchSession", f"会话不存在: {sid}")
        pending = _Pending(
            request=envelope, entry=entry, sid=sid, spec=spec, started=time.monotonic()
        )
        outcome = self._evaluate(pending)
        if outcome is not None:
            return self._reply(envelope, entry, outcome)
        self._pending.append(pending)
        return None

    def poll(self) -> list[Reply]:
        replies: list[Reply] = []
        remaining: list[_Pending] = []
        for pending in self._pending:
            outcome = self._evaluate(pending)
            if outcome is None:
                remaining.append(pending)
            else:
                replies.append(self._reply(pending.request, pending.entry, outcome))
        self._pending = remaining
        return replies

    def on_input(self, key: str, data: bytes) -> None:
        """把一段字节交给会话的写线程（`key` 是 sid）。"""
        runner = self._require_runner(key)
        if not runner.submit_input(data):
            raise ExampleServiceError(f"输入队列已满: {key}")

    def pump(self) -> None:
        """排空每个会话的读桥、推进退出检测，并记下活动时间。"""
        now = time.monotonic()
        for session in self._registry.list():
            runner = self._runners.get(session.uid)
            if runner is not None:
                runner.pump()
            self._note_activity(session, now)

    def shutdown(self) -> None:
        """关掉所有会话。可能长时间阻塞——调用方会带超时。"""
        for sid in list(self._entries):
            try:
                self._remove(sid)
            except Exception:
                _logger.exception("关闭会话失败 sid=%s", sid)
        deadline = time.monotonic() + _RELEASE_JOIN_SECONDS
        for thread in self._releasing:
            thread.join(max(0.0, deadline - time.monotonic()))

    # ════════════════════════════════════════════════════════════
    # 命令
    # ════════════════════════════════════════════════════════════

    def _cmd_status(self, op: Mapping[str, Any]) -> _Produced:
        # 除监听地址外都能自答：处理层就活在守护进程里，pid 与启动时刻是一回事。
        status = DaemonStatus(
            pid=os.getpid(),
            started_at=self._started_stamp,
            uptime=round(time.monotonic() - self._started_at, 3),
            sessions=len(self._entries),
            listen=self._listen(),
        )
        return _Produced(status.to_dict())

    def _cmd_create_shell_terminal(self, op: Mapping[str, Any]) -> _Produced:
        argv = _argv(op, "shell") or default_shell()
        return self._create(_text(op, "sid"), PTY, argv, op)

    def _cmd_create_terminal(self, op: Mapping[str, Any]) -> _Produced:
        return self._create(_text(op, "sid"), PTY, _argv(op, "command", required=True), op)

    def _cmd_list_sessions(self, op: Mapping[str, Any]) -> _Produced:
        wanted = _str_list(op, "sids")
        tags = _str_list(op, "tags")
        if wanted:
            infos = [self._info(self._entries[sid]) for sid in wanted if sid in self._entries]
        elif tags:
            wanted_tags = set(tags)
            infos = [self._info(e) for e in self._entries.values() if wanted_tags & set(e.tags)]
        else:
            infos = [self._info(e) for e in self._entries.values()]
        return _Produced({"sessions": [info.to_dict() for info in infos]})

    def _cmd_remove_session(self, op: Mapping[str, Any]) -> _Produced:
        sids = _str_list(op, "sids")
        tags = _str_list(op, "tags")
        if not sids and not tags:
            raise BadRequest("remove_session 必须给 sids 或 tags，不能什么都不给")
        if sids:
            targets = [sid for sid in sids if sid in self._entries]
        else:
            wanted = set(tags)
            targets = [e.sid for e in self._entries.values() if wanted & set(e.tags)]
        for sid in targets:
            self._remove(sid)
        return _Produced({"removed": targets})

    def _cmd_input(self, op: Mapping[str, Any]) -> _Produced:
        sid = _text(op, "sid")
        text = _text(op, "text")
        session = self._session(sid)
        data = text.encode(session.spec.encoding)
        self.on_input(sid, data)
        return _Produced({"sid": sid, "written": len(data)})

    def _cmd_read(self, op: Mapping[str, Any]) -> _Produced:
        sid = _text(op, "sid")
        raw_mode = _text(op, "mode", required=False) or ReadMode.SCREEN.value
        try:
            mode = ReadMode(raw_mode)
        except ValueError:
            raise BadRequest(f"未知读取模式: {raw_mode}") from None
        session = self._session(sid)
        if mode in (ReadMode.SCREEN, ReadMode.IMAGE, ReadMode.TEXT) and not isinstance(
            session, TerminalSession
        ):
            raise BadRequest(f"{session.mode} 会话没有屏幕，用 mode=bytes")
        if mode is ReadMode.SCREEN:
            return _Produced(
                {"sid": sid, "mode": mode.value, "text": session.screen_text()}, Kind.SCREEN
            )
        if mode is ReadMode.TEXT:
            return _Produced(
                {"sid": sid, "mode": mode.value, "text": _tail_lines(session.full_text(), op)},
                Kind.TEXT,
            )
        if mode is ReadMode.IMAGE:
            scale = _number(op, "scale", 1.0)
            blob = session.render_image(scale=scale, fmt="png")
            return _Produced(
                {"sid": sid, "mode": mode.value, "scale": scale, "bytes": len(blob)},
                Kind.IMAGE,
                binary=blob,
            )
        stream = _stream(op)
        blob = _tail_bytes(session, stream, op)
        return _Produced(
            {
                "sid": sid,
                "mode": mode.value,
                "stream": stream.value,
                "bytes": len(blob),
                "tail": _optional_int(op, "tail"),
            },
            Kind.BYTES,
            stream.value,
            blob,
        )

    def _cmd_resize(self, op: Mapping[str, Any]) -> _Produced:
        sid = _text(op, "sid")
        cols = _int(op, "cols")
        rows = _int(op, "rows")
        self._session(sid).resize(cols, rows)
        return _Produced({"sid": sid, "cols": cols, "rows": rows})

    # ════════════════════════════════════════════════════════════
    # 会话生命周期
    # ════════════════════════════════════════════════════════════

    def _create(
        self, sid: str, mode: str, argv: tuple[str, ...], op: Mapping[str, Any]
    ) -> _Produced:
        if sid in self._entries:
            raise SessionExisted(f"sid 已存在: {sid}")
        spec = SessionSpec(
            mode=mode,
            argv=argv,
            cols=_int(op, "cols", default=_DEFAULT_COLS),
            rows=_int(op, "rows", default=_DEFAULT_ROWS),
            cwd=_text(op, "cwd", required=False),
            env=_env(op),
        )
        session = self._registry.create(spec)
        self._entries[sid] = _Entry(sid=sid, uid=session.uid, tags=_tags(op))
        try:
            session.start()
            runner = SessionRunner(session)
            runner.start()
        except Exception:
            self._entries.pop(sid, None)
            self._registry.close(session.uid)
            raise
        self._runners[session.uid] = runner
        self._activity[session.uid] = _Activity(at=time.monotonic())
        _logger.info("会话已创建 sid=%s argv=%s", sid, list(argv))
        return _Produced({"sid": sid, "mode": mode, "argv": list(argv)})

    def _remove(self, sid: str) -> None:
        """摘除会话。

        **释放分两阶段**（架构设计 §11）：同步摘除——立刻从目录与注册表消失，不再被
        轮询——再把耗时的关闭扔进线程。宿主关闭可能长时间阻塞，压在所有者循环上会冻住
        所有会话。
        """
        entry = self._entries.pop(sid)
        self._activity.pop(entry.uid, None)
        # 这个会话名下的等待**不删**：留着让 `_evaluate` 判成 gone 并给出答复，
        # 删掉就等于让客户端永远等下去。
        runner = self._runners.pop(entry.uid, None)
        session = self._registry.detach(entry.uid)
        self._releasing = [t for t in self._releasing if t.is_alive()]

        def _release() -> None:
            try:
                session.close()
            finally:
                if runner is not None:
                    runner.stop()

        thread = threading.Thread(target=_release, name=f"release-{sid}", daemon=True)
        thread.start()
        self._releasing.append(thread)
        _logger.info("会话已摘除 sid=%s", sid)

    def _session(self, sid: str) -> Session:
        return self._registry.get(self._entry(sid).uid)

    def _entry(self, sid: str) -> _Entry:
        entry = self._entries.get(sid)
        if entry is None:
            raise NoSuchSession(f"会话不存在: {sid}")
        return entry

    def _require_runner(self, sid: str) -> SessionRunner:
        runner = self._runners.get(self._entry(sid).uid)
        if runner is None:
            raise NoSuchSession(f"会话不可写: {sid}")
        return runner

    # ════════════════════════════════════════════════════════════
    # 应答与等待判定
    # ════════════════════════════════════════════════════════════

    def _command(self, envelope: Envelope) -> _Command | Reply:
        try:
            command = Command(envelope.type)
        except ValueError:
            return self._fail(envelope, "UnknownCommand", f"未知命令: {envelope.type}")
        entry = self._commands.get(command)
        if entry is None:
            return self._fail(envelope, "UnknownCommand", f"命令未实现: {envelope.type}")
        return entry

    def _reply(
        self, request: Envelope, entry: _Command, outcome: dict[str, Any] | None = None
    ) -> Reply:
        """跑产出函数并组响应。产出时抛错一律变成失败响应，不让连接挂在那里。"""
        try:
            produced = entry.produce(request.payload.op)
        except (ExampleServiceError, CoreError) as exc:
            return self._fail(request, type(exc).__name__, str(exc))
        data = dict(produced.data)
        if outcome is not None:
            data["wait"] = outcome
        envelope = ok_response(request.type, request.mid, data, kind=produced.kind)
        return Reply(envelope, stream=produced.stream, binary=produced.binary, request=request)

    def _fail(self, request: Envelope, code: str, message: str) -> Reply:
        return Reply(failed_response(request.type, request.mid, code, message), request=request)

    def _evaluate(self, pending: _Pending) -> dict[str, Any] | None:
        """判定一个等待；返回结果字典，未命中返回 None。

        优先级固定：`crashed` → `ended` → `idle` → `timeout`。退出排在静默之前是刻意的
        ——程序都结束了，就没有"再等等看"的意义。
        """
        spec = pending.spec
        elapsed = time.monotonic() - pending.started
        entry = self._entries.get(pending.sid)
        if entry is None:
            return {"reason": "gone", "elapsed": round(elapsed, 4)}
        session = self._registry.get(entry.uid)
        exit_code = session.exit_code
        drained = session.drained
        # 结束以 drained 为准：进程退出与尾部输出到达之间有竞态，只等退出会丢掉最后一段输出。
        if drained and exit_code is not None:
            if spec.crashed and exit_code != 0:
                return _outcome("crashed", elapsed, exit_code, drained)
            if spec.ended and exit_code == 0:
                return _outcome("ended", elapsed, exit_code, drained)
        if spec.idle is not None:
            activity = self._activity.get(entry.uid)
            quiet = time.monotonic() - (activity.at if activity else pending.started)
            if quiet >= spec.idle:
                return _outcome("idle", elapsed, exit_code, drained)
        if spec.timeout is not None and elapsed >= spec.timeout:
            return _outcome("timeout", elapsed, exit_code, drained)
        return None

    def _info(self, entry: _Entry) -> SessionInfo:
        session = self._registry.get(entry.uid)
        terminal = isinstance(session, TerminalSession)
        return SessionInfo(
            sid=entry.sid,
            mode=session.mode,
            state=str(session.state),
            running=session.exit_code is None,
            drained=session.drained,
            exit_code=session.exit_code,
            tags=entry.tags,
            cols=session.spec.cols if terminal else None,
            rows=session.spec.rows if terminal else None,
        )

    def _note_activity(self, session: Session, now: float) -> None:
        activity = self._activity.get(session.uid)
        if activity is None:
            return
        if isinstance(session, TerminalSession):
            # 先比日志偏移：没有新字节就不可能有画面变化，别白渲染一整屏文本
            end = session.journal.end_offset
            if activity.offsets.get(Stream.STDOUT) == end:
                return
            activity.offsets[Stream.STDOUT] = end
            try:
                text = session.screen_text()
            except CoreError:  # 宿主已释放
                return
            if text != activity.screen:
                activity.screen = text
                activity.at = now
            return
        for stream in session.streams():
            try:
                end = session.journal_for(stream).end_offset
            except CoreError:
                return
            if activity.offsets.get(stream) != end:
                activity.offsets[stream] = end
                activity.at = now


def _outcome(reason: str, elapsed: float, exit_code: int | None, drained: bool) -> dict[str, Any]:
    return {
        "reason": reason,
        "elapsed": round(elapsed, 4),
        "exit_code": exit_code,
        "drained": drained,
    }


# ════════════════════════════════════════════════════════════════════
# 参数解析（对端送来的东西一律先验再用）
# ════════════════════════════════════════════════════════════════════


def _text(op: Mapping[str, Any], key: str, *, required: bool = True) -> str:
    value = op.get(key)
    if value is None:
        if required:
            raise BadRequest(f"缺少参数 {key}")
        return ""
    if not isinstance(value, str):
        raise BadRequest(f"参数 {key} 必须是字符串")
    return value


def _int(op: Mapping[str, Any], key: str, default: int | None = None) -> int:
    value = op.get(key)
    if value is None:
        if default is None:
            raise BadRequest(f"缺少参数 {key}")
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise BadRequest(f"参数 {key} 必须是整数")
    return value


def _number(op: Mapping[str, Any], key: str, default: float) -> float:
    value = op.get(key)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BadRequest(f"参数 {key} 必须是数字")
    return float(value)


def _optional_int(op: Mapping[str, Any], key: str) -> int | None:
    value = op.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise BadRequest(f"参数 {key} 必须是正整数")
    return value


def _tail_bytes(session: Session, stream: Stream, op: Mapping[str, Any]) -> bytes:
    """`tail` 给"只要最后 N 字节"用。

    字节流全量可能到日志预算（默认 1MB），客户端每刷新一次就整段搬过去会把界面拖死——
    而它绝大多数时候只想看尾巴。
    """
    tail = _optional_int(op, "tail")
    if tail is None:
        return session.read_all(stream)
    end = session.journal_for(stream).end_offset
    return session.read_range(max(0, end - tail), end, stream)


def _tail_lines(text: str, op: Mapping[str, Any]) -> str:
    """`lines` 给"只要最后 N 行"用（全量输出同理可能很大）。"""
    lines = _optional_int(op, "lines")
    if lines is None:
        return text
    return "".join(text.splitlines(keepends=True)[-lines:])


def _str_list(op: Mapping[str, Any], key: str) -> list[str]:
    value = op.get(key)
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise BadRequest(f"参数 {key} 必须是字符串数组")
    return list(value)


def _tags(op: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(_str_list(op, "tags"))


def _env(op: Mapping[str, Any]) -> dict[str, str]:
    value = op.get("env")
    if value is None:
        return {}
    if not isinstance(value, dict) or any(
        not isinstance(k, str) or not isinstance(v, str) for k, v in value.items()
    ):
        raise BadRequest("参数 env 必须是字符串到字符串的映射")
    return dict(value)


def _argv(op: Mapping[str, Any], key: str, *, required: bool = False) -> tuple[str, ...]:
    """命令行：接受字符串（按 shell 语义拆分）或字符串数组。"""
    value = op.get(key)
    if value is None:
        if required:
            raise BadRequest(f"缺少参数 {key}")
        return ()
    if isinstance(value, str):
        parts = shlex.split(value)
        if not parts:
            raise BadRequest(f"参数 {key} 拆不出可执行的命令")
        return tuple(parts)
    if isinstance(value, list) and value and all(isinstance(item, str) for item in value):
        return tuple(value)
    raise BadRequest(f"参数 {key} 必须是非空字符串或字符串数组")


def _stream(op: Mapping[str, Any]) -> Stream:
    raw = _text(op, "stream", required=False) or Stream.STDOUT.value
    try:
        return Stream(raw)
    except ValueError:
        raise BadRequest(f"未知输出流: {raw}") from None
