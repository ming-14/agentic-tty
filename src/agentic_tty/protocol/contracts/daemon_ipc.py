"""守护进程 ↔ 下游消费者：**uid 级**原语。

守护进程不认识 `sid`——那是下游消费者的语义。因此这一侧所有的键一律是 `uid`；
返回条件、会话标签这些属于消费者的东西，一概不在这里。

原语直接对着 core 的能力：起会话、读、改尺寸、看状态、**订阅**。**写不占命令**——它走
字节帧上行（`STREAM_STDIN`），一个字节都不用进 JSON。

**订阅**（`subscribe` / `unsubscribe`）：
- `subscribe` 的 `op` 是 `{uid, stream?, cursor?}`，答复的 `data` 是 `{sub_id, offset, lossy}`。
- **订阅 id 就是那条 `subscribe` 请求的 `mid`**（连接内唯一），客户端按它分派推送。
- 推送全是**下行帧**：字节走字节帧（`key` = 订阅 id），说不清的走控制帧（`mid` = 订阅 id，
  `type` 取 `Event`）。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from ..errors import MessageError


class Command(StrEnum):
    """守护进程侧原语。"""

    DAEMON_STATUS = "daemon_status"
    SHUTDOWN_DAEMON = "shutdown_daemon"
    """让守护进程收尾退出。**进程级动作**：连着它的所有消费者的会话都会一起结束。
    答复先回，收尾随后开始（见 `daemon/kernel.py`）。"""
    CREATE_SESSION = "create_session"
    CLOSE_SESSION = "close_session"
    LIST_SESSIONS = "list_sessions"
    READ_SESSION = "read_session"
    RESIZE_SESSION = "resize_session"
    SUBSCRIBE = "subscribe"
    UNSUBSCRIBE = "unsubscribe"


class Event(StrEnum):
    """订阅推送里**非字节**的通知（以控制帧发，`mid` = 订阅 id）。

    字节本身走字节帧；这里只放字节流说不清的三件事。
    """

    RESIZE = "resize"
    """`offset` 起（含）的字节按 `cols × rows` 解释——raw 订阅者据此在字节流里插帧。"""
    RESYNC = "resync"
    """接下来是**重同步快照**；`lossy` 为真表示前面那段字节找不回来了。"""
    ENDED = "ended"
    """会话已排空，不会再有字节；订阅到此为止。"""


# 流标签：线上的 1 字节取值 ↔ 流名。流名必须与 core 的 `Stream` 取值一致
# （协议层不 import core）。
STREAM_STDOUT = 0x01
STREAM_STDERR = 0x02
STREAM_STDIN = 0x03
"""上行字节的方向标签。输入只有一个方向，这个标签只为把上行与下行同型帧区分开。"""

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
    SVG = "svg"
    """可见屏幕的矢量源码（pty 专属）——客户端自己铺画布、栅格化。"""
    IMAGE = "image"
    """可见屏幕的位图（png），以字节帧返回（pty 专属）。"""
    BYTES = "bytes"
    """字节流全量（原始真源），以字节帧返回。"""


@dataclass(frozen=True, slots=True)
class SessionRef:
    """一条会话的 uid 级快照。

    **只有 `uid`，没有 `sid`、没有 `tags`**：那两样都是消费者的语义。

    `cols` / `rows` 只有终端会话才有——客户端要靠它把屏幕缩放到自己的画布。
    `members` 是进程树成员数，`None` = 观测不到（未启动 / 没有作业对象 / 已关闭），
    与"确实 0 个"分开。
    """

    uid: str
    command: str
    mode: str
    state: str
    running: bool
    drained: bool
    exit_code: int | None = None
    cols: int | None = None
    rows: int | None = None
    members: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "uid": self.uid,
            "command": self.command,
            "mode": self.mode,
            "state": self.state,
            "running": self.running,
            "drained": self.drained,
            "exit_code": self.exit_code,
            "cols": self.cols,
            "rows": self.rows,
            "members": self.members,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> SessionRef:
        if not isinstance(raw, dict):
            raise MessageError("会话快照必须是对象")
        try:
            return cls(
                uid=str(raw["uid"]),
                command=str(raw["command"]),
                mode=str(raw["mode"]),
                state=str(raw["state"]),
                running=bool(raw["running"]),
                drained=bool(raw["drained"]),
                exit_code=_optional_int(raw.get("exit_code")),
                cols=_optional_int(raw.get("cols")),
                rows=_optional_int(raw.get("rows")),
                members=_optional_int(raw.get("members")),
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
    endpoint: str
    """接入点地址；不挂接入点时为空串。"""

    def to_dict(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "started_at": self.started_at,
            "uptime": self.uptime,
            "sessions": self.sessions,
            "endpoint": self.endpoint,
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
                endpoint=str(raw["endpoint"]),
            )
        except KeyError as exc:
            raise MessageError(f"守护进程状态缺少字段: {exc}") from exc


def _optional_int(value: Any) -> int | None:
    return None if value is None else int(value)
