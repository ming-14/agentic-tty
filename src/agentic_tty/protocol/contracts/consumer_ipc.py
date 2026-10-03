"""下游消费者 ↔ 它的客户端：**sid 级**编排。

`sid` 在这一侧才诞生——守护进程侧载的是 `uid`。返回条件（`Condition`）也是这一侧的
词汇：等待引擎住在这里，守护进程根本不知道"条件"为何物。

命令集是**对客户端**的完整清单（起 shell / 起终端 / 起子进程 / 读 / 写 / 配置 / 通知），
与 `daemon_ipc` 的 uid 级原语**刻意各留一份**，哪怕名字有重合。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from ..errors import MessageError


class Command(StrEnum):
    """下游消费者对客户端提供的命令。"""

    # 生命周期
    DAEMON_STATUS = "get_daemon_status"

    # 管理
    CREATE_SHELL_TERMINAL = "create_shell_terminal"
    CREATE_TERMINAL = "create_terminal"
    CREATE_SUBPROCESS = "create_subprocess"
    LIST_SESSIONS = "list_sessions"
    REMOVE_SESSION = "remove_session"
    CLOSE_WINDOW = "close_window"
    RUN_WORKFLOW = "run_workflow"

    # 操作
    INPUT_INTO_TERMINAL = "input_into_terminal"
    ADVANCED_INPUT_INTO_TERMINAL = "advanced_input_into_terminal"
    READ_TERMINAL = "read_terminal"
    RESIZE_TERMINAL = "resize_terminal"
    INPUT_INTO_SUBPROCESS = "input_into_subprocess"
    READ_SUBPROCESS = "read_subprocess"
    SAVE_SCREEN = "save_screen"
    SEND_MOUSE_EVENT_TO_TERMINAL = "send_mouse_event_to_terminal"

    # 配置
    SET_GLOBAL_DEFAULT_VALUE = "set_global_default_value"
    LIST_GLOBAL_DEFAULT_VALUES = "list_global_default_values"
    SET_SESSION_VALUE = "set_session_value"
    LIST_SESSION_VALUES = "list_session_values"

    # 通知
    VIEW_NOTICE = "view_notice"


class ViewMode(StrEnum):
    """`read_terminal` 要哪个视图。

    取值与 `daemon_ipc.ReadMode` 一致是刻意的（同一套视图语义），但**两份分区各留一份、
    互不 import**——改这条边界不牵动那条。
    """

    SCREEN = "screen"
    TEXT = "text"
    IMAGE = "image"
    BYTES = "bytes"


class Condition(StrEnum):
    """返回条件。求值优先级由等待引擎固定，不由请求方指定。

    这里只放**已实现**的条件；完整词汇（`notify` / `matched` / `echo` / `gui` /
    `cancelled`）未实现的暂不列出——客户端用了会拿到"未知返回条件"的明确报错，
    而不是被静默忽略后等错东西。
    """

    ENDED = "ended"
    CRASHED = "crashed"
    IDLE = "idle"
    TIMEOUT = "timeout"


@dataclass(frozen=True, slots=True)
class SessionInfo:
    """一条会话的对外快照。

    **只有 `sid`，没有 `uid`**：`uid` 是核心层内部的标识，不该出现在这一侧。

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
                exit_code=_optional_int(raw.get("exit_code")),
                tags=tuple(str(t) for t in raw.get("tags") or ()),
                cols=_optional_int(raw.get("cols")),
                rows=_optional_int(raw.get("rows")),
            )
        except KeyError as exc:
            raise MessageError(f"会话信息缺少字段: {exc}") from exc


def _optional_int(value: Any) -> int | None:
    return None if value is None else int(value)
