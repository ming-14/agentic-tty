"""MCP 服务端：把**本进程里的核心层**暴露成工具。

与 `daemon_test_mcp_server` 那台是同一套工具面，差别只在后端——这台没有守护进程、没有
连接，工具直接调 core（见 `core.py`）。工具函数的 docstring 就是给模型看的说明书；
失败抛 `ToolError`——SDK 会把它连同消息包成 `isError` 的工具结果交给模型，不会崩掉服务端。

**uid 只在工具面上露前几位**：core 侧一律是完整 uuid4，收进来的短 uid 在 `_resolve()`
里按当前会话表还原。撞号不做处理，取第一个匹配。
"""

from __future__ import annotations

import asyncio
import functools
import json
from collections.abc import Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from ...foundation.errors import AgenticTtyError
from .core import Core

_MODE = "pty"
"""只提供 pywezterm 版的终端会话——别的模式不进这个台子。"""
_UID_CHARS = 4
"""工具面上露出的 uid 长度。"""


def build_server(core: Core) -> MCPServer:
    """把本进程里的核心层装成一个 MCP 服务端。"""

    @asynccontextmanager
    async def lifespan(_server: MCPServer):
        """跑起来时挂上推进循环——会话的输出靠它一直从宿主流进来。"""
        pump = asyncio.create_task(core.pump_forever())
        try:
            yield core
        finally:
            core.close_all()  # 置停机标志 + 收掉全部会话
            await pump  # 推进循环看到标志就退出，别把它悬在关掉的循环上

    server = MCPServer(
        "agentic-tty",
        instructions=(
            "终端会话在本进程里直接托管：create_terminal 开一个，read_terminal 看它现在的样子，"
            "write_input 往里敲命令，list_terminals 列出全部，close_terminal 收掉。"
            f"uid 是完整 uuid 的前 {_UID_CHARS} 位，照原样传回来。"
        ),
        lifespan=lifespan,
    )

    @server.tool()
    @_guarded
    async def list_terminals() -> str:
        """列出所有终端：uid、命令、模式、状态、退出码、尺寸。"""
        return _json([_brief(session) for session in core.list()])

    @server.tool()
    @_guarded
    async def create_terminal(
        terminal: str = "", cwd: str = "", cols: int = 80, rows: int = 24
    ) -> str:
        """开一个终端，返回它的快照（含 uid）。

        terminal：要跑的程序（留空 = 平台默认 shell）。cwd：工作目录（留空 = 本进程当前
        目录）。cols / rows：初始屏幕尺寸。
        """
        created = core.create(_MODE, terminal.split(), cwd=cwd or None, cols=cols, rows=rows)
        return _json(_brief(created))

    @server.tool()
    @_guarded
    async def read_terminal(uid: str) -> str:
        """取终端当前可见屏幕的文本。"""
        return core.read(_resolve(core, uid))

    @server.tool()
    @_guarded
    async def write_input(uid: str, text: str, enter: bool = True) -> str:
        """往终端里写输入；enter=True 时补一个回车（终端会话的回车是 CR）。"""
        data = text.encode() + (b"\r" if enter else b"")
        if not core.write(_resolve(core, uid), data):
            raise ToolError("输入被拒收：整块超过输入队列硬上限，没有收下")
        return _json({"sent": len(data)})

    @server.tool()
    @_guarded
    async def close_terminal(uid: str) -> str:
        """关掉一个终端。"""
        full = _resolve(core, uid)
        core.close(full)
        return _json({"closed": _short(full)})

    return server


def _guarded(fn: Callable[..., Awaitable[str]]) -> Callable[..., Awaitable[str]]:
    """工具统一把 core 的**可预期失败**转成 `ToolError`——不然模型只看到 SDK 的通用报错。

    可预期 = `AgenticTtyError`（命令不存在、目录没权限、会话没了…）与 `OSError`（标准库
    的类型，语义同样是"操作没做成"）；其余异常是真出 bug，照旧穿透给 SDK 记堆栈。
    """

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> str:
        try:
            return await fn(*args, **kwargs)
        except (AgenticTtyError, OSError) as exc:
            raise ToolError(f"{type(exc).__name__}: {exc}") from exc

    return wrapper


def _short(uid: str) -> str:
    return uid[:_UID_CHARS]


def _brief(session: Mapping[str, Any]) -> dict[str, Any]:
    """会话快照 → 工具面上的形态：uid 只留前几位。"""
    return {**session, "uid": _short(str(session["uid"]))}


def _resolve(core: Core, uid: str) -> str:
    """短 uid → 完整 uid：core 只认完整的。

    无状态——按**当前**会话表匹配，不留表也就没有脏项。撞号取第一个；空 uid 不算匹配
    （不然会随手命中第一个终端）。
    """
    for session in core.list():
        full = str(session["uid"])
        if uid and full.startswith(uid):
            return full
    raise ToolError(f"没有这个终端: {uid}")


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)
