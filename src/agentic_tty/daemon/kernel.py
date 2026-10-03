"""守护进程的**默认请求处理层**：uid 级请求 → core 操作。

它把 `daemon_ipc` 的命令翻成 core 调用——这就是守护进程对外的能力面。**本包只有它和
`assembly.py` 碰 core**：`server.py` / `access_point.py` / `handler.py` / `platform/` 是"机制"，
一行 core 都不碰，换掉这一层机制不用改（白名单由 AST 断言按文件强制）。

接缝上的请求对象是 `WireRequest`，答复必须原样把**同一个对象**带回去——路由靠它。
"""

from __future__ import annotations

import os
import time
from collections.abc import Mapping
from typing import Any, cast

from ..core.errors import CoreError
from ..core.ports import PTY, SUBPROCESS, SessionSpec, Stream
from ..core.process.session import ProcessSession
from ..core.runtime.runtime import Runtime
from ..core.runtime.shell import default_shell
from ..core.session.base import Session
from ..core.session.registry import SessionKind, SessionRegistry
from ..core.session.state import SessionState
from ..core.terminal.session import TerminalSession
from ..foundation.ids import now_timestamp
from ..foundation.logs import get_logger
from ..protocol.contracts.daemon_ipc import Command, ReadMode, SessionRef, stream_tag
from ..protocol.envelope import Envelope
from ..protocol.response import failed_response, ok_response
from .access_point import ByteChunk, WireRequest
from .handler import Reply

_logger = get_logger("daemon.kernel")


def _text(op: Mapping[str, Any], key: str, default: str = "") -> str:
    value = op.get(key)
    return default if value is None else str(value)


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

    def __init__(self, registry: SessionRegistry | None = None, *, listen: str = "") -> None:
        self._runtime = Runtime(
            registry
            or SessionRegistry(
                kinds={PTY: SessionKind(TerminalSession), SUBPROCESS: SessionKind(ProcessSession)}
            )
        )
        self._listen = listen
        self._started_at = now_timestamp()
        self._started_monotonic = time.monotonic()

    # ════════════════════════════════════════════════════════════
    # RequestHandler（只在所有者线程上被调用，因此不需要锁）
    # ════════════════════════════════════════════════════════════

    def handle(self, request: object) -> Reply | None:
        wire = cast(WireRequest, request)
        try:
            answer = self._dispatch(wire.envelope)
        except (CoreError, OSError, ValueError) as exc:
            return Reply(request=wire, answer=_failed(wire.envelope, exc))
        return Reply(request=wire, answer=answer)

    def poll(self) -> list[Reply]:
        return []

    def pending(self) -> int:
        return 0

    def failure(self, request: object, error: BaseException) -> Reply:
        wire = cast(WireRequest, request)
        return Reply(request=wire, answer=_failed(wire.envelope, error))

    def on_input(self, key: str, data: bytes) -> None:
        """字节帧上行 = 往那个会话写字节——**键就是 uid**，标签在输入方向不用。"""
        try:
            self._runtime.send_input(key, data)
        except CoreError as exc:  # 会话刚被关掉
            _logger.warning("上行字节无人接收 uid=%s: %s", key, exc)

    def pump(self) -> None:
        self._runtime.pump_all()

    def shutdown(self) -> None:
        self._runtime.close_all()

    # ════════════════════════════════════════════════════════════
    # 命令
    # ════════════════════════════════════════════════════════════

    def _dispatch(self, envelope: Envelope) -> Envelope | ByteChunk:
        command = envelope.type
        op = envelope.payload.op
        if command == Command.DAEMON_STATUS:
            return self._status(envelope)
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
        raise ValueError(f"未知命令: {command}")

    def _status(self, envelope: Envelope) -> Envelope:
        return ok_response(
            envelope.type,
            envelope.mid,
            {
                "pid": os.getpid(),
                "started_at": self._started_at,
                "uptime": round(time.monotonic() - self._started_monotonic, 3),
                "sessions": len(self._runtime.list()),
                "listen": self._listen,
            },
        )

    def _create(self, envelope: Envelope, op: Mapping[str, Any]) -> Envelope:
        argv = tuple(str(item) for item in (op.get("argv") or ())) or default_shell()
        spec = SessionSpec(
            mode=_text(op, "mode"),
            argv=argv,
            cols=int(op.get("cols") or 80),
            rows=int(op.get("rows") or 24),
            # 请求里给了就在那儿跑；没给 = 继承守护进程的目录（守护进程自己的目录由入口的
            # `--cwd` 定，见 `daemon/__main__.py`）
            cwd=_text(op, "cwd") or None,
        )
        # 会话与驱动一起建、一起起；起不来时 Runtime 自己把会话收掉，这里不留半个。
        session = self._runtime.create(spec)
        return ok_response(envelope.type, envelope.mid, {"session": self._ref(session).to_dict()})

    def _read(self, envelope: Envelope, op: Mapping[str, Any]) -> Envelope | ByteChunk:
        session = self._runtime.get(_text(op, "uid"))
        mode = ReadMode(_text(op, "mode"))
        stream = Stream(_text(op, "stream", Stream.STDOUT.value))
        if mode is ReadMode.BYTES:
            # 只取尾部——`read_all` 会把整个保留区复制一遍，客户端每轮都读它。
            journal = session.journal_for(stream)
            tail = int(op.get("tail") or 0)
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
            scale = float(op.get("scale") or 1.0)
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
        cols = int(op.get("cols") or 0)
        rows = int(op.get("rows") or 0)
        if cols <= 0 or rows <= 0:
            raise ValueError(f"尺寸不合法: {cols}×{rows}")
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
