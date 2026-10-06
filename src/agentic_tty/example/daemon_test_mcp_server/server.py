"""MCP 服务端：把守护进程的能力暴露成工具。

每个工具 = 一次到守护进程的请求（或一帧上行字节），答复按 `protocol.response` 那套约定
拆开。工具函数的 docstring 就是给模型看的说明书。失败抛 `ToolError`——SDK 会把它连同
消息包成 `isError` 的工具结果交给模型，不会崩掉服务端。

**uid 只在工具面上露前几位**：守护进程侧一律是完整 uuid4，收进来的短 uid 在 `_resolve()`
里按当前会话表还原。撞号不做处理，取第一个匹配。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from ...protocol.contracts.daemon_ipc import Command, ReadMode
from ...protocol.frame import BytesFrame
from ...protocol.response import data_of, error_of, is_ok
from ...transport.errors import TransportError
from .supervisor import Supervisor

_MODE = "pty"
"""只提供 pywezterm 版的终端会话——别的模式不进这个台子。"""
_UID_CHARS = 4
"""工具面上露出的 uid 长度。"""


def build_server(supervisor: Supervisor) -> MCPServer:
    """把守护进程的能力装成一个 MCP 服务端。"""
    server = MCPServer(
        "agentic-tty",
        instructions=(
            "终端由守护进程托管：create_terminal 开一个，read_terminal 看它现在的样子，"
            "write_input 往里敲命令，list_terminals 列出全部，close_terminal 收掉。"
            f"uid 是完整 uuid 的前 {_UID_CHARS} 位，照原样传回来。"
        ),
    )

    @server.tool()
    def list_terminals() -> str:
        """列出所有终端：uid、命令、模式、状态、退出码、尺寸。"""
        return _json([_brief(session) for session in _sessions(supervisor)])

    @server.tool()
    def create_terminal(terminal: str = "", cwd: str = "", cols: int = 80, rows: int = 24) -> str:
        """开一个终端，返回它的快照（含 uid）。

        terminal：要跑的程序（留空 = 平台默认 shell）。cwd：工作目录（留空 = 守护进程的
        启动目录）。cols / rows：初始屏幕尺寸。
        """
        op: dict[str, Any] = {
            "mode": _MODE,
            "argv": terminal.split(),
            "cols": cols,
            "rows": rows,
        }
        if cwd:
            op["cwd"] = cwd
        return _json(_brief(_call(supervisor, Command.CREATE_SESSION, op)["session"]))

    @server.tool()
    def read_terminal(uid: str) -> str:
        """取终端当前可见屏幕的文本。"""
        op = {"uid": _resolve(supervisor, uid), "mode": ReadMode.SCREEN.value}
        return str(_call(supervisor, Command.READ_SESSION, op).get("text") or "")

    @server.tool()
    def write_input(uid: str, text: str, enter: bool = True) -> str:
        """往终端里写输入；enter=True 时补一个回车（终端会话的回车是 CR）。"""
        data = text.encode() + (b"\r" if enter else b"")
        _write(supervisor, _resolve(supervisor, uid), data)
        return _json({"sent": len(data)})

    @server.tool()
    def close_terminal(uid: str) -> str:
        """关掉一个终端。"""
        full = _resolve(supervisor, uid)
        _call(supervisor, Command.CLOSE_SESSION, {"uid": full})
        return _json({"closed": _short(full)})

    return server


def _sessions(supervisor: Supervisor) -> list[dict[str, Any]]:
    return _call(supervisor, Command.LIST_SESSIONS).get("sessions") or []


def _short(uid: str) -> str:
    return uid[:_UID_CHARS]


def _brief(session: Mapping[str, Any]) -> dict[str, Any]:
    """会话快照 → 工具面上的形态：uid 只留前几位。"""
    return {**session, "uid": _short(str(session["uid"]))}


def _resolve(supervisor: Supervisor, uid: str) -> str:
    """短 uid → 完整 uid：守护进程只认完整的。

    无状态——按**当前**会话表匹配，不留表也就没有脏项。撞号取第一个；空 uid 不算匹配
    （不然会随手命中第一个终端）。
    """
    for session in _sessions(supervisor):
        full = str(session["uid"])
        if uid and full.startswith(uid):
            return full
    raise ToolError(f"没有这个终端: {uid}")


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def _call(supervisor: Supervisor, command: str, op: dict[str, Any] | None = None) -> dict:
    """发一条请求，拆开成功 / 失败；失败转成 `ToolError`。"""
    try:
        answer = supervisor.call(command, op)
    except TransportError as exc:
        raise ToolError(f"守护进程不可用: {exc}") from exc
    if isinstance(answer, BytesFrame):
        raise ToolError(f"{command} 回了字节帧，这里不该有")
    if not is_ok(answer):
        failure = error_of(answer)
        raise ToolError(f"{failure.code}: {failure.message}" if failure else "未知错误")
    return data_of(answer)


def _write(supervisor: Supervisor, uid: str, data: bytes) -> None:
    """送一帧上行字节——它不占命令，没有答复可拆，失败同样是 `ToolError`。"""
    try:
        supervisor.write(uid, data)
    except TransportError as exc:
        raise ToolError(f"守护进程不可用: {exc}") from exc
