"""会话消息 schema：命令名、返回条件、以及跨机的数据结构。

线格式上的东西一旦发出去就改不了，所以这里只放**两端都要认**的定义：
命令类型、条件词汇、会话信息、守护进程状态。参数取值不做枚举——它们由各命令
自己声明，加参数不该动协议版本。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from .envelope import Envelope, make_response
from .errors import MessageError


class Command(StrEnum):
    """统一命令。接入层（MCP / CLI / Web）与进程内调用共用同一套名字。"""

    DAEMON_STATUS = "get_daemon_status"
    CREATE_SHELL_TERMINAL = "create_shell_terminal"
    CREATE_TERMINAL = "create_terminal"
    LIST_SESSIONS = "list_sessions"
    REMOVE_SESSION = "remove_session"
    INPUT_INTO_TERMINAL = "input_into_terminal"
    READ_TERMINAL = "read_terminal"
    RESIZE_TERMINAL = "resize_terminal"


class Condition(StrEnum):
    """返回条件。求值优先级由等待引擎固定，不由请求方指定。

    这里只放**已实现**的条件；完整词汇见架构设计 §6.1，未实现的（`notify` /
    `matched` / `echo` / `gui` / `cancelled`）暂不列出——客户端用了会拿到
    "未知返回条件"的明确报错，而不是被静默忽略后等错东西。
    """

    ENDED = "ended"
    CRASHED = "crashed"
    IDLE = "idle"
    TIMEOUT = "timeout"


class ReadMode(StrEnum):
    """`read_terminal` 的请求参数：要哪个视图。

    取值与 `Kind` 一致是刻意的（读什么视图直接决定返回走哪个呈现通道），但两者是
    **不同位置上的字段**——一个是请求参数，一个是信封上的呈现意图——所以各留一个，
    不合并。

    `IMAGE` 是给**纯客户端**准备的：客户端只依赖 `protocol + transport`，它没有终端
    模型、自己渲染不出屏幕，只能让守护进程渲染好再把位图送过来。
    """

    SCREEN = "screen"
    """可见屏幕纯文本（pty 专属）。"""
    TEXT = "text"
    """全量输出：含滚动历史的可见文本（pty 专属）。"""
    IMAGE = "image"
    """可见屏幕的位图（png），以字节帧返回（pty 专属）。"""
    BYTES = "bytes"
    """字节流全量（原始真源），以字节帧返回。"""


class Kind(StrEnum):
    """`kind` 字段：呈现意图，表示层据此选渲染通道。"""

    TEXT = "text"
    SCREEN = "screen"
    IMAGE = "image"
    BYTES = "bytes"


# 终端模型的字符格基准像素。`read_terminal` 的 image 模式由守护进程按这个基准渲染，
# 客户端拿会话的 cols / rows 估算缩放；两端都得能拿到，所以放在共享契约里，
# 而不是各自硬编码一份。
DEFAULT_CELL_WIDTH = 8
DEFAULT_CELL_HEIGHT = 17


@dataclass(frozen=True, slots=True)
class SessionInfo:
    """一条会话的对外快照。

    **只有 `sid`，没有 `uid`**：`uid` 是核心层内部的标识，出了 service 就该消失。

    `cols` / `rows` 只有终端会话才有——客户端要靠它把屏幕缩放到自己的画布。
    """

    sid: str
    mode: str
    state: str
    running: bool
    drained: bool
    exit_code: int | None = None
    tags: tuple[str, ...] = ()
    cols: int | None = None
    rows: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "sid": self.sid,
            "mode": self.mode,
            "state": self.state,
            "running": self.running,
            "drained": self.drained,
            "exit_code": self.exit_code,
            "tags": list(self.tags),
            "cols": self.cols,
            "rows": self.rows,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> SessionInfo:
        if not isinstance(raw, dict):
            raise MessageError("会话信息必须是对象")
        try:
            return cls(
                sid=str(raw["sid"]),
                mode=str(raw["mode"]),
                state=str(raw["state"]),
                running=bool(raw["running"]),
                drained=bool(raw["drained"]),
                exit_code=raw.get("exit_code"),
                tags=tuple(str(t) for t in raw.get("tags") or ()),
                cols=_optional_int(raw.get("cols")),
                rows=_optional_int(raw.get("rows")),
            )
        except KeyError as exc:
            raise MessageError(f"会话信息缺少字段: {exc}") from exc


@dataclass(frozen=True, slots=True)
class DaemonStatus:
    """守护进程快照。"""

    pid: int
    started_at: str
    uptime: float
    sessions: int
    listen: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "started_at": self.started_at,
            "uptime": self.uptime,
            "sessions": self.sessions,
            "listen": self.listen,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> DaemonStatus:
        if not isinstance(raw, dict):
            raise MessageError("守护进程状态必须是对象")
        try:
            return cls(
                pid=int(raw["pid"]),
                started_at=str(raw["started_at"]),
                uptime=float(raw["uptime"]),
                sessions=int(raw["sessions"]),
                listen=str(raw["listen"]),
            )
        except KeyError as exc:
            raise MessageError(f"守护进程状态缺少字段: {exc}") from exc


@dataclass(frozen=True, slots=True)
class Failure:
    """失败的响应载荷。"""

    code: str
    message: str
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, **self.extra}


def ok_response(
    type_: str, mid: str, data: dict[str, Any] | None = None, *, kind: Kind = Kind.TEXT
) -> Envelope:
    """成功响应。"""
    return make_response(type_, mid, output={"ok": True, "data": data or {}}, kind=kind.value)


def failed_response(
    type_: str, mid: str, code: str, message: str, *, extra: dict[str, Any] | None = None
) -> Envelope:
    """失败响应。`code` 用错误类名，客户端据此分流，不靠文案匹配。"""
    failure = Failure(code=code, message=message, extra=extra or {})
    return make_response(type_, mid, output={"ok": False, "error": failure.to_dict()})


def is_ok(envelope: Envelope) -> bool:
    return bool(envelope.payload.output.get("ok"))


def data_of(envelope: Envelope) -> dict[str, Any]:
    """取成功响应的数据体；不是成功响应就抛。"""
    output = envelope.payload.output
    if not output.get("ok"):
        raise ValueError(f"不是成功响应: {output.get('error')}")
    data = output.get("data")
    return data if isinstance(data, dict) else {}


def error_of(envelope: Envelope) -> Failure | None:
    """取失败响应的错误；不是失败响应返回 None。"""
    output = envelope.payload.output
    if output.get("ok"):
        return None
    raw = output.get("error")
    if not isinstance(raw, dict):
        return Failure(code="MalformedError", message="响应缺少 error 字段")
    code = str(raw.get("code") or "UnknownError")
    message = str(raw.get("message") or "")
    extra = {k: v for k, v in raw.items() if k not in ("code", "message")}
    return Failure(code=code, message=message, extra=extra)


def _optional_int(value: Any) -> int | None:
    return None if value is None else int(value)
