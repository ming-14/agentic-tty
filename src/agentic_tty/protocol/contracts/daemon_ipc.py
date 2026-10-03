"""守护进程 ↔ 下游消费者：**uid 级**原语。

守护进程不认识 `sid`——那是下游消费者的语义。因此这一侧所有的键一律是 `uid`；
返回条件、会话标签这些属于消费者的东西，一概不在这里。

原语直接对着 core 的能力：起会话、读写、订阅、改尺寸、看状态。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from ..errors import MessageError


class Command(StrEnum):
    """守护进程侧原语。"""

    DAEMON_STATUS = "daemon_status"
    CREATE_SESSION = "create_session"
    CLOSE_SESSION = "close_session"
    LIST_SESSIONS = "list_sessions"
    READ_SESSION = "read_session"
    WRITE_SESSION = "write_session"
    RESIZE_SESSION = "resize_session"
    SUBSCRIBE = "subscribe"
    UNSUBSCRIBE = "unsubscribe"


# 流标签：线上的 1 字节取值 ↔ 流名。流名必须与 core 的 `Stream` 取值一致
# （协议层不 import core，靠测试对齐）。
STREAM_STDOUT = 0x01
STREAM_STDERR = 0x02

_STREAM_NAMES = {STREAM_STDOUT: "stdout", STREAM_STDERR: "stderr"}
_STREAM_TAGS = {name: tag for tag, name in _STREAM_NAMES.items()}


def stream_tag(name: str) -> int:
    """流名 → 帧里的标签。"""
    tag = _STREAM_TAGS.get(name)
    if tag is None:
        raise MessageError(f"未知流: {name!r}")
    return tag


def stream_name(tag: int) -> str:
    """帧里的标签 → 流名。"""
    name = _STREAM_NAMES.get(tag)
    if name is None:
        raise MessageError(f"未知流标签: {tag:#x}")
    return name


class ReadMode(StrEnum):
    """`read_session` 要哪个视图。

    `IMAGE` 是给**纯客户端**准备的：它只依赖 `protocol + transport`，没有终端模型、
    自己渲染不出屏幕，只能让守护进程渲染好再把位图送过来。
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


# 终端模型的字符格基准像素。`read_session` 的 image 模式由守护进程按这个基准渲染，
# 客户端拿会话的 cols / rows 估算缩放；两端都得能拿到，所以放在共享契约里，
# 而不是各自硬编码一份。
DEFAULT_CELL_WIDTH = 8
DEFAULT_CELL_HEIGHT = 17


@dataclass(frozen=True, slots=True)
class SessionRef:
    """一条会话的 uid 级快照。

    **只有 `uid`，没有 `sid`、没有 `tags`**：那两样都是消费者的语义。

    `cols` / `rows` 只有终端会话才有——客户端要靠它把屏幕缩放到自己的画布。
    """

    uid: str
    mode: str
    state: str
    running: bool
    drained: bool
    exit_code: int | None = None
    cols: int | None = None
    rows: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "uid": self.uid,
            "mode": self.mode,
            "state": self.state,
            "running": self.running,
            "drained": self.drained,
            "exit_code": self.exit_code,
            "cols": self.cols,
            "rows": self.rows,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> SessionRef:
        if not isinstance(raw, dict):
            raise MessageError("会话快照必须是对象")
        try:
            return cls(
                uid=str(raw["uid"]),
                mode=str(raw["mode"]),
                state=str(raw["state"]),
                running=bool(raw["running"]),
                drained=bool(raw["drained"]),
                exit_code=_optional_int(raw.get("exit_code")),
                cols=_optional_int(raw.get("cols")),
                rows=_optional_int(raw.get("rows")),
            )
        except KeyError as exc:
            raise MessageError(f"会话快照缺少字段: {exc}") from exc


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


def _optional_int(value: Any) -> int | None:
    return None if value is None else int(value)
