"""把会话渲染成界面要显示的文本与行值——只碰 core，不碰 Tk。

界面（`example/ui/`）不认识会话，认识会话的这一层就是这里：视图、格栅、原始字节、
进程、事件、状态栏那行，全是从会话与驱动快照直接算出来的。
"""

from __future__ import annotations

from collections.abc import Iterable

from ...core.runtime.monitor import windows_of
from ...core.runtime.runner import Exited, Ingested, PumpEvent, StreamEof
from ...core.session.base import Session
from ...core.terminal.session import TerminalSession

RAW_TAIL = 2000


def _safe(getter) -> str:
    """终端视图取值；宿主已关闭等异常转成一行提示。"""
    try:
        return getter()
    except Exception as exc:
        return f"<不可用: {exc}>"


def metadata(session: Session) -> str:
    """终端标题 / cwd（仅 Pty 会话有；取不到就不显示）。"""
    if not isinstance(session, TerminalSession):
        return ""
    try:
        meta = session.metadata()
    except Exception:  # 宿主已关闭等
        return ""
    parts = [text for text in (meta.title, meta.cwd) if text]
    return f" · {' · '.join(parts)}" if parts else ""


def status_text(session: Session) -> str:
    """状态栏那行。"""
    drained = "已排空" if session.drained else "进行中"
    return (
        f"{session.spec.argv[0]} · {session.mode} · {session.state} · "
        f"{drained} · exit={session.exit_code} · uid={session.uid[:8]}"
        f"{metadata(session)}"
    )


def input_text(depth: int, drained: bool) -> str:
    """状态栏里的输入队列那一段：积压字节数；越过软水位时点出来。"""
    return f" · 输入 {depth}B" + ("" if drained else "（越软水位）")


def event_lines(events: Iterable[PumpEvent]) -> list[str]:
    """把一轮事件渲染成行——`pump_all()` 交出的东西，消费者靠它知道"哪一路进了哪段"。"""
    lines = []
    for event in events:
        if isinstance(event, Ingested):
            size = event.end - event.start
            lines.append(f"进 {event.stream} [{event.start}, {event.end}) +{size}B\n")
        elif isinstance(event, StreamEof):
            lines.append(f"排空 {event.stream}\n")
        elif isinstance(event, Exited):
            lines.append(f"退出 code={event.exit_code}\n")
    return lines


def view_text(session: Session, *, full: bool) -> str:
    """「视图」页：终端会话出屏幕文本，其余会话出双流。`full` = 含滚动历史。"""
    if not isinstance(session, TerminalSession):
        return _streams_text(session)
    return _safe(session.full_text if full else session.screen_text)


def cells_text(session: Session) -> str:
    """字符格栅：宽字符占两格、续格为空串——空串画成 `·` 才看得出来。"""
    if not isinstance(session, TerminalSession):
        return "（只有终端会话有屏幕）"
    try:
        rows = session.screen_cells()
    except Exception as exc:  # 宿主已关闭等
        return f"<不可用: {exc}>"
    return "\n".join("".join(cell or "·" for cell in row) for row in rows)


def _streams_text(session: Session) -> str:
    parts = []
    for stream in session.streams():
        text = session.read_all(stream).decode("utf-8", errors="replace")
        parts.append(f"── {stream} ──\n{text}")
    return "\n".join(parts)


def raw_text(session: Session) -> str:
    """「原始字节」页：每路只取尾部（`read_all` 会把整个保留区复制一遍）+ 重建字节。"""
    lines = []
    for stream in session.streams():
        end = session.journal_for(stream).end_offset
        tail = session.read_range(max(0, end - RAW_TAIL), end, stream)
        lines.append(f"── {stream} ──\n{tail!r}")
    if isinstance(session, TerminalSession):
        lines.append(f"── 重建字节（rebuild_bytes）──\n{_rebuild_text(session)}")
    return "\n".join(lines)


def _rebuild_text(session: Session) -> str:
    """重建字节：喂进一个空终端模型即可还原当前状态，订阅者重同步用它。"""
    try:
        data = session.rebuild_bytes()
    except Exception as exc:  # 宿主已关闭等
        return f"<不可用: {exc}>"
    prefix = "…" if len(data) > RAW_TAIL else ""
    return f"{prefix}{data[-RAW_TAIL:]!r}（共 {len(data)} 字节）"


def process_members(session: Session) -> tuple[int, ...] | None:
    """进程树成员；观测不到（未启动 / 没有作业对象 / 已关闭）返回 None。

    `descendants()` 是**轮询式**观测，这里每刷一次界面查一次；比对前后两次就能看出
    "谁起来了、谁没了"——判定留给命令层，核心层只出快照。
    """
    try:
        return session.descendants()
    except Exception:  # CoreError / MonitorUnavailable
        return None


def processes_text(session: Session) -> str:
    """「进程」页：成员 pid + 该会话名下的可见窗口。"""
    host = session.host
    root = host.pid if host is not None else None
    lines = [
        f"模式 {session.mode} · 状态 {session.state}"
        f" · 退出码 {'-' if session.exit_code is None else session.exit_code}",
        f"根进程 pid {root if root is not None else '-'}",
    ]
    members = process_members(session)
    if members is None:
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


def row_values(session: Session) -> tuple[str | int | None, ...]:
    """会话表一行的值（列序见 `ui.SessionTree.COLUMNS`）。

    `None` = 观测不到（退出码还没拿到 / 进程树看不见），占位符 `-` 由会话表画；**0 是
    有效值**（正常退出、0 个成员），不能跟 `None` 混为一谈。
    """
    members = process_members(session)
    return (
        session.spec.argv[0] if session.spec.argv else "",
        session.mode,
        session.state,
        session.exit_code,
        None if members is None else len(members),
    )


def screen_png(session: Session, scale: float) -> bytes:
    """屏幕位图（PNG）；渲染不了就抛（只有终端会话有屏幕），由调用方决定怎么显示。"""
    return session.render_image(scale=scale, fmt="png")
