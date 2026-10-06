"""直连 core 的 MCP 验证台：真 MCP 服务端对象 + 真 pty，五个工具走一遍。

它没有守护进程、没有连接，所以起台子不需要任何 fixture——装一个 `Core` 就够。推进循环
由服务端的 lifespan 挂在事件循环上，这里没跑 MCP 传输，于是手动 `pump()`。
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import fields

import pytest

pytest.importorskip("mcp")

from mcp.server.mcpserver.exceptions import ToolError

from agentic_tty.example.core_test_mcp_server.core import Core
from agentic_tty.example.core_test_mcp_server.server import build_server
from agentic_tty.protocol.contracts.daemon_ipc import SessionRef

_TIMEOUT = 20.0


def _text(result) -> str:
    return "".join(part.text for part in result.content)


def _pty_available() -> bool:
    try:
        from agentic_tty.core.runtime.pywezterm_pty.host import require_pywezterm

        require_pywezterm()
        return True
    except Exception:
        return False


async def _pump_until(core: Core, predicate, timeout: float = _TIMEOUT) -> bool:
    """手动推进一轮轮，直到条件成立——没有 MCP 传输就没有 lifespan 那个推进任务。"""
    deadline = time.monotonic() + timeout
    while True:
        core.pump()
        if await predicate():
            return True
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(0.02)


@pytest.mark.skipif(not _pty_available(), reason="pywezterm 不可用")
def test_tool_surface_drives_a_real_terminal():
    """开 → 写 → 读 → 列 → 关一路走通；uid 在工具面上只露前 4 位，坏 uid 报错。"""
    asyncio.run(_scenario())


async def _scenario() -> None:
    core = Core()
    server = build_server(core)
    try:
        assert [tool.name for tool in await server.list_tools()] == [
            "list_terminals",
            "create_terminal",
            "read_terminal",
            "write_input",
            "close_terminal",
        ]

        created = json.loads(
            _text(await server.call_tool("create_terminal", {"cols": 80, "rows": 24}))
        )
        # 与守护进程那台**同一套工具面**：快照字段对得上，uid 只留前 4 位
        assert set(created) == {field.name for field in fields(SessionRef)}
        uid = created["uid"]
        assert len(uid) == 4

        await server.call_tool("write_input", {"uid": uid, "text": "echo mark-core"})

        async def echoed() -> bool:
            return "mark-core" in _text(await server.call_tool("read_terminal", {"uid": uid}))

        assert await _pump_until(core, echoed), "屏幕上没出现敲进去的那条命令"

        listed = json.loads(_text(await server.call_tool("list_terminals", {})))
        assert [session["uid"] for session in listed] == [uid]

        closed = json.loads(_text(await server.call_tool("close_terminal", {"uid": uid})))
        assert closed == {"closed": uid}
        assert json.loads(_text(await server.call_tool("list_terminals", {}))) == []

        with pytest.raises(ToolError, match="没有这个终端"):
            await server.call_tool("read_terminal", {"uid": "zzzz"})
    finally:
        core.close_all()
