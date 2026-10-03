"""本层的最小请求处理层：把一条请求翻成对核心层的操作。

**本包唯一碰核心层的地方。** 守护进程不认识报文——它只把请求投进来、把答复带回去
（接缝对它是不透明的），所以请求与答复就地定义成简单类型，不必牵线协议。

它不是正式用例层：没有返回条件与等待引擎、没有通知、没有插件。够用的那一小块 =
把核心层的能力（会话 CRUD、屏幕与字节视图、进程树、尺寸）经接缝摆到界面上。

会话与驱动**成对**交给 `core.runtime.Runtime`；本层只额外持 `sid ↔ uid` 的映射——
那是消费者语义（见架构设计 §7）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, cast

from ...core.errors import CoreError
from ...core.ports import PTY, SUBPROCESS, SessionSpec
from ...core.process.session import ProcessSession
from ...core.runtime.input_queue import InputVerdict
from ...core.runtime.monitor import windows_of
from ...core.runtime.runtime import Runtime
from ...core.runtime.shell import default_shell
from ...core.session.base import Session
from ...core.session.registry import SessionKind, SessionRegistry
from ...core.terminal.session import TerminalSession
from ...daemon.handler import Reply
from ...foundation.logs import get_logger

_logger = get_logger("example.daemon_test.handler")

_RAW_TAIL = 2000
"""原始字节页每路只取尾部——`read_all` 会把整个保留区复制一遍。"""


@dataclass(frozen=True, slots=True)
class Request:
    """一条请求；守护进程只当它是不透明对象，不看这些字段。"""

    action: str
    sid: str = ""
    argv: tuple[str, ...] = ()
    mode: str = PTY
    cols: int = 80
    rows: int = 24
    scale: float = 1.0


@dataclass(frozen=True, slots=True)
class Answer:
    """一条答复；同样不透明。`data["blob"]` 里可以放位图这种二进制。"""

    ok: bool
    data: dict[str, Any] = field(default_factory=dict)
    error: str = ""


class KernelHandler:
    """`RequestHandler` 的最小实现。所有方法都只在所有者线程上被调用，因此无需加锁。"""

    def __init__(self, registry: SessionRegistry | None = None) -> None:
        # registry 可注入：测试用假宿主装配，生产用默认（真宿主）。
        self._runtime = Runtime(
            registry
            or SessionRegistry(
                kinds={PTY: SessionKind(TerminalSession), SUBPROCESS: SessionKind(ProcessSession)},
            )
        )
        self._uids: dict[str, str] = {}
        self.pumps = 0
        """推进过的轮数——验证"所有者循环确实在跑"。"""

    # ════════════════════════════════════════════════════════════
    # RequestHandler（只在所有者线程上被调用）
    # ════════════════════════════════════════════════════════════

    def handle(self, request: object) -> Reply | None:
        req = cast(Request, request)
        try:
            data = self._dispatch(req)
        except (CoreError, OSError, ValueError) as exc:
            return Reply(request=request, answer=self._fail(exc))
        return Reply(request=request, answer=Answer(True, data=data))

    def poll(self) -> list[Reply]:
        return []

    def pending(self) -> int:
        return 0

    def failure(self, request: object, error: BaseException) -> Reply:
        return Reply(request=request, answer=self._fail(error))

    def on_input(self, key: str, data: bytes) -> None:
        """字节帧本身就是一次操作：直接把这段写进那个会话。

        输入队列按字节计量，判定三态：`HOLD` 表示该让发送方本端排队、`REJECTED` 表示
        超过硬上限——**下发 InputHold / 断开连接是消费者连接层的活**，本层只记日志。
        """
        verdict = self._runtime.send_input(self._uid_of(key), data)
        if verdict is InputVerdict.REJECTED:
            _logger.warning("上行字节超过硬上限，已拒绝 sid=%s bytes=%d", key, len(data))

    def pump(self) -> None:
        self.pumps += 1
        self._runtime.pump_all()

    def shutdown(self) -> None:
        """收尾：清掉会话目录并释放全部会话（释放可能长时间阻塞，调用方带超时）。"""
        self._uids.clear()
        self._runtime.close_all()

    # ════════════════════════════════════════════════════════════
    # 动作
    # ════════════════════════════════════════════════════════════

    def _dispatch(self, req: Request) -> dict[str, Any]:
        if req.action == "create":
            return self._create(req)
        if req.action == "close":
            self._close(req.sid)
            return {"sid": req.sid}
        if req.action == "resize":
            return self._resize(req)
        if req.action == "list":
            return {"sessions": [self._summary(sid) for sid in self._uids]}
        if req.action == "detail":
            return self._detail(req.sid)
        if req.action == "image":
            return {"sid": req.sid, "blob": self._terminal(req.sid).render_image(
                scale=req.scale, fmt="png"
            )}
        if req.action == "stats":
            return {"pumps": self.pumps, "sessions": len(self._uids)}
        raise ValueError(f"未知动作: {req.action}")

    def _create(self, req: Request) -> dict[str, Any]:
        if not req.sid:
            raise ValueError("create 必须给 sid")
        if req.sid in self._uids:
            raise ValueError(f"sid 已存在: {req.sid}")
        argv = req.argv or default_shell()  # 不给命令就起一个平台默认 shell
        # 会话与驱动一起建、一起起；起不来时 Runtime 自己把会话收掉，这里不留半个。
        session = self._runtime.create(SessionSpec(mode=req.mode, argv=argv))
        self._uids[req.sid] = session.uid
        return self._summary(req.sid)

    def _close(self, sid: str) -> None:
        """摘除会话：**同步摘除 + 线程里释放**（两阶段都在 `Runtime` 里）。

        宿主关闭在部分平台上会长时间阻塞（Windows 上要等控制台客户端退出，可达数百秒），
        压在所有者线程上会冻住所有会话。摘除之后把耗时的释放交给别的线程。
        """
        uid = self._uids.pop(sid, None)
        if uid is None:
            raise CoreError(f"会话不存在: {sid}")
        self._runtime.close(uid)

    def _resize(self, req: Request) -> dict[str, Any]:
        self._terminal(req.sid).resize(req.cols, req.rows)
        return {"sid": req.sid, "cols": req.cols, "rows": req.rows}

    # ════════════════════════════════════════════════════════════
    # 返回数据（都在这里合成好，界面只管显示）
    # ════════════════════════════════════════════════════════════

    def _summary(self, sid: str) -> dict[str, Any]:
        session = self._session(sid)
        return {
            "sid": sid,
            "command": session.spec.argv[0] if session.spec.argv else "",
            "mode": session.mode,
            "state": str(session.state),
            "exit_code": session.exit_code,
            "members": _members(session),
        }

    def _detail(self, sid: str) -> dict[str, Any]:
        session = self._session(sid)
        terminal = isinstance(session, TerminalSession)
        data: dict[str, Any] = {
            "sid": sid,
            "command": session.spec.argv[0] if session.spec.argv else "",
            "mode": session.mode,
            "state": str(session.state),
            "drained": session.drained,
            "exit_code": session.exit_code,
            "is_terminal": terminal,
            "cols": session.cols if terminal else None,
            "rows": session.rows if terminal else None,
            "title": None,
            "cwd": None,
            "members": _members(session),
            "view": _view_text(session),
            "raw": _raw_text(session),
            "procs": _procs_text(session),
            "svg": None,
            "offset": 0,
        }
        if terminal:
            data["offset"] = session.journal.end_offset
            try:
                data["svg"] = session.render_svg()
            except Exception as exc:  # 宿主已关闭等
                data["svg_error"] = str(exc)
            try:
                meta = session.metadata()
                data["title"], data["cwd"] = meta.title, meta.cwd
            except Exception:  # 宿主已关闭等
                pass
        return data

    # ════════════════════════════════════════════════════════════
    # 内部
    # ════════════════════════════════════════════════════════════

    def _uid_of(self, sid: str) -> str:
        uid = self._uids.get(sid)
        if uid is None:
            raise CoreError(f"会话不存在: {sid}")
        return uid

    def _session(self, sid: str) -> Session:
        return self._runtime.get(self._uid_of(sid))

    def _terminal(self, sid: str) -> TerminalSession:
        session = self._session(sid)
        if not isinstance(session, TerminalSession):
            raise CoreError(f"{session.mode} 会话没有屏幕")
        return session

    @staticmethod
    def _fail(error: BaseException) -> Answer:
        return Answer(False, error=f"{type(error).__name__}: {error}")


def _members(session: Session) -> int | None:
    """进程树成员数；观测不到（未启动 / 没有作业对象 / 已关闭）返回 None。"""
    try:
        return len(session.descendants())
    except Exception:  # CoreError / MonitorUnavailable
        return None


def _view_text(session: Session) -> str:
    """「视图」页：pty 给可见屏幕，子进程给每路字节流（解成文本）。"""
    if isinstance(session, TerminalSession):
        try:
            return session.screen_text()
        except Exception as exc:  # 宿主已关闭等
            return f"<屏幕不可用: {exc}>"
    parts = []
    for stream in session.streams():
        text = session.read_all(stream).decode("utf-8", errors="replace")
        parts.append(f"── {stream} ──\n{text}")
    return "\n".join(parts)


def _raw_text(session: Session) -> str:
    lines = []
    for stream in session.streams():
        end = session.journal_for(stream).end_offset
        tail = session.read_range(max(0, end - _RAW_TAIL), end, stream)
        lines.append(f"── {stream} ──\n{tail!r}")
    return "\n".join(lines)


def _procs_text(session: Session) -> str:
    host = session.host
    root = host.pid if host is not None else None
    exit_code = "-" if session.exit_code is None else session.exit_code
    lines = [
        f"模式 {session.mode} · 状态 {session.state} · 退出码 {exit_code}",
        f"根进程 pid {root if root is not None else '-'}",
    ]
    try:
        members = session.descendants()
    except Exception:  # CoreError / MonitorUnavailable
        lines.append("进程树成员：观测不到（未启动 / 没有作业对象 / 已关闭）")
        return "\n".join(lines)
    lines.append(f"进程树成员 {len(members)} 个（不含根进程）：")
    lines.extend(f"  pid {pid}" for pid in members)
    wanted = {root} if root is not None else set()
    windows = windows_of(wanted | set(members))
    if windows is None:  # 本平台查不到，与"确实没有窗口"区分开
        lines.append("可见窗口：本平台不支持探测")
    else:
        lines.append(f"可见窗口 {len(windows)} 个：")
        lines.extend(f'  pid {w.pid} · "{w.title}"' for w in windows)
    return "\n".join(lines)
