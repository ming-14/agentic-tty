"""示例层的 Tk 管理台：接入核心层接口，手动起会话、看屏幕、发输入。

    python -m agentic_tty.example

**Tk 的 mainloop 就是所有者线程**：界面回调与 `SessionRunner.pump()` 都跑在同一
线程，所以这里不需要任何锁——这正是核心层"单线程所有者"约定带来的好处。
宿主的读由 core.runtime 的读线程代劳（`SessionRunner`），界面线程从不阻塞。

模式三选一：`fake` 跑示例假程序（命令框下拉即假程序名）；`pty` / `subprocess`
跑真命令（命令框直接输入）。

右侧「屏幕」/「SVG 源码」页是 **pty 专属**（其他模式藏掉）：`image` 格式走
`Session.render_image`（终端模型直接出位图），`svg` 格式走 `Session.render_svg`
再经 resvg 栅格化——**Tk 的 PhotoImage 只吃位图，没有 SVG 解码器**。屏幕按画布
大小缩放铺满，不出滚动条。

「进程」页**所有模式都有**：会话树里那列"进程"是进程树成员数，页内列出成员 pid，
以及（Windows）该会话名下探测到的可见窗口。
"""

from __future__ import annotations

import base64
import re
import shlex
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

try:
    import resvg_py
except ImportError as exc:  # 依赖缺失就说清楚怎么补，不静默降级
    raise ImportError("Tk 管理台渲染 SVG 需要 resvg-py：pip install -e .[gui]") from exc

from ...core.runtime.monitor import windows_of
from ...core.runtime.runner import SessionRunner
from ...core.session.base import Session
from ...core.session.registry import SessionRegistry
from ...core.terminal.session import TerminalSession
from ...foundation.logs import get_logger
from .programs import PROGRAMS
from .sessions import ExampleMode, create_registry, session_spec

_logger = get_logger("example.core_test.gui")

_TICK_MS = 20
# 每多少个 tick 刷一次界面（20ms × 8 ≈ 160ms，避免文本频繁重排）
_REFRESH_EVERY = 8
_RAW_TAIL = 2000
# 保存 PNG 用的固定缩放（屏幕页显示时会按画布大小另算）
_EXPORT_SCALE = 2.0
_FORMAT_IMAGE = "image"
_FORMAT_SVG = "svg"


def _svg_size(svg: str) -> tuple[int, int] | None:
    """渲染结果自带的像素尺寸（1.0 倍）——根元素上写着 width / height。"""
    root = svg.split(">", 1)[0]
    width = re.search(r'\bwidth="([\d.]+)"', root)
    height = re.search(r'\bheight="([\d.]+)"', root)
    if width is None or height is None:
        return None
    return int(float(width.group(1))), int(float(height.group(1)))


def _metadata_of(session: Session) -> str:
    """终端标题 / cwd（仅 Pty 会话有；取不到就不显示）。"""
    if not isinstance(session, TerminalSession):
        return ""
    try:
        meta = session.metadata()
    except Exception:  # 宿主已关闭等
        return ""
    parts = [text for text in (meta.title, meta.cwd) if text]
    return f" · {' · '.join(parts)}" if parts else ""


class App:
    """管理台。

    会话表就是 core 的注册表（`SessionRegistry`）：`uid → 会话` 与模式标签都由它持有，
    界面只额外持有每个会话的运行时驱动（读 / 写线程）。
    """

    def __init__(self, root: tk.Tk) -> None:
        self._root = root
        self._registry: SessionRegistry = create_registry()
        self._runners: dict[str, SessionRunner] = {}
        self._selected: str | None = None
        self._ticks = 0
        # 屏幕视图：屏幕 / 格式 / 画布尺寸没变就不重渲染——位图渲染不便宜
        self._rendered_key: tuple[str, int, str, float] | None = None
        self._photo: tk.PhotoImage | None = None
        self._svg_source: str | None = None  # None = 该会话没有屏幕视图
        self._screen_note = ""  # 没有屏幕视图时的提示文字
        self._tick_job: str | None = None  # 挂起的定时任务；收尾必须先取消，否则销毁后仍会触发
        self._build_ui()
        self._select_session(None)  # 初始无会话：pty 专属控件按此状态摆好
        self._tick_job = self._root.after(_TICK_MS, self._tick)

    # ════════════════════════════════════════════════════════════
    # 界面
    # ════════════════════════════════════════════════════════════

    def _build_ui(self) -> None:
        self._root.title("agentic-tty · core 管理台")
        self._root.geometry("1040x660")

        top = ttk.Frame(self._root, padding=(8, 6))
        top.pack(fill=tk.X)
        ttk.Label(top, text="模式").pack(side=tk.LEFT)
        self._mode = tk.StringVar(value=ExampleMode.FAKE.value)
        for text, value in (
            ("fake", ExampleMode.FAKE.value),
            ("pty", ExampleMode.PTY.value),
            ("subprocess", ExampleMode.SUBPROCESS.value),
        ):
            ttk.Radiobutton(
                top, text=text, value=value, variable=self._mode, command=self._on_mode_change
            ).pack(side=tk.LEFT, padx=(4, 0))
        ttk.Label(top, text="   命令").pack(side=tk.LEFT)
        self._command = ttk.Combobox(top, values=sorted(PROGRAMS), width=18)
        self._command.set("repl")
        self._command.pack(side=tk.LEFT)
        ttk.Button(top, text="创建会话", command=self._create_session).pack(side=tk.LEFT, padx=6)
        ttk.Button(top, text="关闭选中", command=self._close_selected).pack(side=tk.LEFT)
        ttk.Label(
            top,
            text="（fake 下拉选假程序；pty / subprocess 直接输入真命令）",
            foreground="#888780",
        ).pack(side=tk.LEFT, padx=6)

        body = ttk.Panedwindow(self._root, orient=tk.HORIZONTAL)
        body.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 6))

        left = ttk.Frame(body)
        body.add(left, weight=1)
        columns = ("command", "mode", "state", "exit", "procs")
        self._tree = ttk.Treeview(left, columns=columns, show="headings", selectmode="browse")
        for col, text, width in (
            ("command", "命令", 90),
            ("mode", "模式", 80),
            ("state", "状态", 70),
            ("exit", "退出码", 60),
            ("procs", "进程", 50),
        ):
            self._tree.heading(col, text=text)
            self._tree.column(col, width=width, anchor=tk.W, stretch=True)
        self._tree.pack(fill=tk.BOTH, expand=True)
        self._tree.bind("<<TreeviewSelect>>", self._on_select)

        right = ttk.Frame(body)
        body.add(right, weight=3)

        self._notebook = ttk.Notebook(right)
        self._notebook.pack(fill=tk.BOTH, expand=True)

        self._image_tab = ttk.Frame(self._notebook)
        format_row = ttk.Frame(self._image_tab)
        format_row.grid(row=0, column=0, sticky="w", padx=6, pady=(4, 2))
        ttk.Label(format_row, text="格式").pack(side=tk.LEFT)
        self._format = tk.StringVar(value=_FORMAT_IMAGE)
        for text, value in (("image", _FORMAT_IMAGE), ("svg", _FORMAT_SVG)):
            ttk.Radiobutton(
                format_row,
                text=text,
                value=value,
                variable=self._format,
                command=self._on_format_change,
            ).pack(side=tk.LEFT, padx=(4, 0))
        ttk.Label(
            format_row,
            text="（image = 模型直接出位图；svg = 出矢量再栅格化）",
            foreground="#888780",
        ).pack(side=tk.LEFT, padx=6)

        # 屏幕铺满画布，不出滚动条
        self._image_canvas = tk.Canvas(self._image_tab, highlightthickness=0)
        self._image_canvas.grid(row=1, column=0, sticky="nsew")
        self._image_tab.rowconfigure(1, weight=1)
        self._image_tab.columnconfigure(0, weight=1)

        self._svg = tk.Text(self._notebook, wrap=tk.NONE, font=("Consolas", 9))
        self._view = tk.Text(self._notebook, wrap=tk.NONE, font=("Consolas", 10))
        self._raw = tk.Text(self._notebook, wrap=tk.NONE, font=("Consolas", 9))
        self._procs = tk.Text(self._notebook, wrap=tk.NONE, font=("Consolas", 10))
        self._notebook.add(self._image_tab, text="屏幕")
        self._notebook.add(self._svg, text="SVG 源码")
        self._notebook.add(self._view, text="视图")
        self._notebook.add(self._raw, text="原始字节")
        self._notebook.add(self._procs, text="进程")

        entry_row = ttk.Frame(right)
        entry_row.pack(fill=tk.X, pady=(6, 0))
        self._input = ttk.Entry(entry_row)
        self._input.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self._input.bind("<Return>", lambda _e: self._send_input(newline=True))
        ttk.Button(entry_row, text="发送", command=self._send_input).pack(side=tk.LEFT, padx=4)
        ttk.Button(
            entry_row, text="发送+换行", command=lambda: self._send_input(newline=True)
        ).pack(side=tk.LEFT)
        # 控制字符编码属于命令层；这里由管理台（命令层占位）自己做，核心层只收字节
        ttk.Button(entry_row, text="Ctrl+C", command=self._send_interrupt).pack(
            side=tk.LEFT, padx=(4, 0)
        )

        size_row = ttk.Frame(right)
        size_row.pack(fill=tk.X, pady=(4, 0))
        ttk.Label(size_row, text="尺寸").pack(side=tk.LEFT)
        self._cols = ttk.Entry(size_row, width=5)
        self._cols.insert(0, "80")
        self._cols.pack(side=tk.LEFT)
        ttk.Label(size_row, text="×").pack(side=tk.LEFT)
        self._rows = ttk.Entry(size_row, width=5)
        self._rows.insert(0, "24")
        self._rows.pack(side=tk.LEFT)
        self._resize_btn = ttk.Button(size_row, text="应用", command=self._resize)
        self._resize_btn.pack(side=tk.LEFT, padx=4)
        self._save_svg_btn = ttk.Button(size_row, text="保存 SVG", command=self._save_svg)
        self._save_svg_btn.pack(side=tk.LEFT, padx=(12, 0))
        self._save_png_btn = ttk.Button(size_row, text="保存 PNG", command=self._save_png)
        self._save_png_btn.pack(side=tk.LEFT, padx=4)

        self._status = tk.StringVar(value="就绪")
        ttk.Label(self._root, textvariable=self._status, relief=tk.SUNKEN, anchor=tk.W).pack(
            fill=tk.X
        )

    # ════════════════════════════════════════════════════════════
    # 驱动循环（所有者线程 = Tk 主线程）
    # ════════════════════════════════════════════════════════════

    def _tick(self) -> None:
        try:
            for runner in list(self._runners.values()):
                runner.pump()
            self._ticks += 1
            if self._ticks % _REFRESH_EVERY == 0:
                self._refresh_tree()
                self._refresh_detail()
        except Exception:
            _logger.exception("驱动循环异常")
        finally:
            self._tick_job = self._root.after(_TICK_MS, self._tick)

    # ════════════════════════════════════════════════════════════
    # 操作
    # ════════════════════════════════════════════════════════════

    def _on_mode_change(self) -> None:
        """切模式时同步命令框：fake 给假程序下拉，真形态清空留给自由输入。"""
        if self._mode.get() == ExampleMode.FAKE.value:
            self._command["values"] = sorted(PROGRAMS)
            if self._command.get() not in PROGRAMS:
                self._command.set("repl")
        else:
            self._command["values"] = []
            self._command.set("")

    def _create_session(self) -> None:
        text = self._command.get().strip()
        if not text:
            messagebox.showwarning("创建会话", "请填命令")
            return
        mode = ExampleMode(self._mode.get())
        # 命令按 shell 语义拆分，支持 "cmd.exe /c dir" 这种整串
        argv = tuple(shlex.split(text)) or (text,)
        try:
            session = self._registry.create(session_spec(mode, argv))
        except Exception as exc:  # 宿主起不来：注册表不会留残骸
            messagebox.showerror("创建会话失败", str(exc))
            return
        runner = SessionRunner(session)
        runner.start()
        self._runners[session.uid] = runner
        self._status.set(f"已创建 {' '.join(argv)}（{mode}）uid={session.uid[:8]}")
        self._refresh_tree()  # 新行先进表，选中它才有意义
        self._select_session(session.uid)

    def _close_selected(self) -> None:
        uid = self._selected
        session = self._registry.find(uid) if uid else None
        if session is None:
            return
        self._registry.close(session.uid)  # 会话收尾：先强杀进程树再关宿主
        runner = self._runners.pop(session.uid, None)
        if runner is not None:
            runner.stop()
        self._status.set(f"已关闭 uid={session.uid[:8]}")
        self._refresh_tree()
        self._select_session(None)

    def _send_input(self, newline: bool = False) -> None:
        runner = self._runners.get(self._selected) if self._selected else None
        if runner is None:
            return
        data = self._input.get().encode() + (b"\n" if newline else b"")
        if not runner.submit_input(data):
            messagebox.showwarning("发送失败", "输入队列已满，请稍后再试")
            return
        self._input.delete(0, tk.END)
        self._status.set(f"已入队 {len(data)} 字节（由写线程写出）")

    def _send_interrupt(self) -> None:
        runner = self._runners.get(self._selected) if self._selected else None
        if runner is None:
            return
        runner.submit_input(b"\x03")  # Ctrl+C
        self._status.set("已入队 Ctrl+C")

    def _resize(self) -> None:
        session = self._registry.find(self._selected) if self._selected else None
        if session is None:
            return
        try:
            cols, rows = int(self._cols.get()), int(self._rows.get())
            session.resize(cols, rows)
        except Exception as exc:
            messagebox.showerror("改尺寸失败", str(exc))
            return
        self._status.set(f"尺寸已改为 {cols}×{rows}")

    def _on_select(self, _event: object = None) -> None:
        """用户改了树的选中项 → 切换当前会话。

        `<<TreeviewSelect>>` 是**异步**派发的，且值没变也照发，程序侧切会话同样会触发它，
        所以先比一次 `_selected`，把非用户发起的那次当成 no-op。
        """
        selection = self._tree.selection()
        uid = selection[0] if selection else None
        if uid != self._selected:
            self._select_session(uid)

    def _select_session(self, uid: str | None) -> None:
        """把当前会话切到 `uid`（无会话传 `None`）：树选中、pty 专属控件、详情一起到位。

        程序侧改选中必须走这里：只改 `_selected` 而不动树，界面就会与实际不一致。
        """
        self._selected = uid
        selected = self._tree.selection()
        if uid is None:
            if selected:
                self._tree.selection_remove(*selected)
        elif uid not in selected:
            self._tree.selection_set(uid)
        self._sync_pty_controls()
        self._refresh_detail()

    def _sync_pty_controls(self) -> None:
        """屏幕页 / SVG 源码页 / 导出按钮 / 尺寸控件都是 pty 专属：其他模式藏掉或禁用。"""
        session = self._registry.find(self._selected) if self._selected else None
        is_pty = isinstance(session, TerminalSession)
        state = "normal" if is_pty else "hidden"
        self._notebook.tab(self._image_tab, state=state)
        self._notebook.tab(self._svg, state=state)
        for widget in (
            self._save_svg_btn,
            self._save_png_btn,
            self._resize_btn,
            self._cols,
            self._rows,
        ):
            widget.state(["!disabled"] if is_pty else ["disabled"])
        if is_pty:
            self._notebook.select(self._image_tab)

    # ════════════════════════════════════════════════════════════
    # 刷新
    # ════════════════════════════════════════════════════════════

    def _refresh_tree(self) -> None:
        """增量刷新：行 iid 就是 uid，只增删行、只在值变了时改写。

        不能全表重建 + `selection_set`：那个事件是异步派发的，会在本函数返回后才被处理，
        于是被当成"用户换了会话"，把用户切走的标签页弹回「屏幕」（见 `_on_select`）。
        """
        sessions = self._registry.list()
        alive = {session.uid for session in sessions}
        for uid in self._tree.get_children():
            if uid not in alive:
                self._tree.delete(uid)
        for index, session in enumerate(sessions):
            values = self._row_values(session)
            if not self._tree.exists(session.uid):
                self._tree.insert("", index, iid=session.uid, values=values)
            elif self._tree.item(session.uid, "values") != tuple(str(v) for v in values):
                self._tree.item(session.uid, values=values)

    def _row_values(self, session: Session) -> tuple[str | int, ...]:
        members = self._processes(session)
        return (
            session.spec.argv[0] if session.spec.argv else "",
            session.mode,
            session.state,
            "-" if session.exit_code is None else session.exit_code,
            "-" if members is None else len(members),
        )

    def _refresh_detail(self) -> None:
        session = self._registry.find(self._selected) if self._selected else None
        if session is None:
            self._set_text(self._view, "")
            self._set_text(self._raw, "")
            self._set_text(self._svg, "")
            self._set_text(self._procs, "")
            self._svg_source = None
            self._screen_note = "未选中会话"
            self._set_image(None, self._screen_note)
            self._rendered_key = None
            return
        self._set_text(self._view, self._render_view(session))
        self._set_text(self._raw, self._render_raw(session))
        self._set_text(self._procs, self._render_processes(session))
        self._refresh_screen_views(session)
        drained = "已排空" if session.drained else "进行中"
        self._status.set(
            f"{session.spec.argv[0]} · {session.mode} · {session.state} · "
            f"{drained} · exit={session.exit_code} · uid={session.uid[:8]}"
            f"{_metadata_of(session)}"
        )

    def _refresh_screen_views(self, session: Session) -> None:
        """屏幕页 / SVG 源码页（pty 专属）：屏幕、格式或画布尺寸没变就跳过重渲染。

        先出 SVG，再从**渲染结果自己**读 1.0 倍的像素尺寸——渲染器把尺寸写在输出里，
        不必去别处问"字符格基准是多少"。出 SVG 只要 ~1ms，贵的是后面的栅格化
        （~25ms），所以缓存挡的是栅格化那一步。
        """
        if not isinstance(session, TerminalSession):
            return
        fmt = self._format.get()
        try:
            svg = session.render_svg()
        except Exception as exc:  # 宿主已关闭等
            self._rendered_key = None
            self._svg_source = None
            self._screen_note = f"<无屏幕视图: {exc}>"
            self._set_text(self._svg, self._screen_note)
            self._set_image(None, self._screen_note)
            return
        size = _svg_size(svg)
        if size is None:
            self._rendered_key = None
            self._svg_source = svg
            self._screen_note = "<渲染结果里没有尺寸，无法铺满画布>"
            self._set_text(self._svg, svg)
            self._set_image(None, self._screen_note)
            return
        scale = self._fit_scale(size)
        key = (session.uid, session.journal.end_offset, fmt, round(scale, 4))
        if key == self._rendered_key:
            return
        self._rendered_key = key
        self._svg_source = svg
        self._screen_note = ""
        self._set_text(self._svg, self._svg_source)
        self._set_image(*self._render_screen(session, fmt, self._svg_source, scale))

    def _fit_scale(self, size: tuple[int, int]) -> float:
        """让屏幕正好铺满画布（不出现滚动条）。画布还没量出尺寸时按 1× 画。"""
        base_width, base_height = size
        if base_width <= 0 or base_height <= 0:
            return 1.0
        width, height = self._image_canvas.winfo_width(), self._image_canvas.winfo_height()
        if width <= 1 or height <= 1:
            return 1.0
        return min(width / base_width, height / base_height)

    def _render_screen(
        self, session: Session, fmt: str, svg: str, scale: float
    ) -> tuple[bytes | None, str]:
        """屏幕位图：`image` 由终端模型直接出，`svg` 先出矢量再经 resvg 栅格化。"""
        try:
            if fmt == _FORMAT_SVG:
                return resvg_py.svg_to_bytes(svg_string=svg, zoom=scale), ""
            return session.render_image(scale=scale, fmt="png"), ""
        except Exception as exc:  # SVG 为空 / 宿主已关闭
            return None, f"<无屏幕位图: {exc}>"

    def _on_format_change(self) -> None:
        self._rendered_key = None  # 格式变了，强制重渲染
        self._refresh_detail()

    def _set_image(self, data: bytes | None, note: str) -> None:
        self._image_canvas.delete("all")
        self._photo = None  # 必须留引用，否则 Tk 会把图回收掉
        if data is None:
            self._image_canvas.create_text(12, 12, anchor="nw", text=note, fill="#888780")
            return
        # Tk 的 PhotoImage 只吃位图（8.6 起原生支持 PNG）；create_image 默认居中
        self._photo = tk.PhotoImage(data=base64.b64encode(data).decode("ascii"))
        self._image_canvas.create_image(
            self._image_canvas.winfo_width() / 2,
            self._image_canvas.winfo_height() / 2,
            image=self._photo,
        )

    def _save_svg(self) -> None:
        if self._svg_source is None:
            messagebox.showinfo("保存 SVG", self._screen_note or "没有 SVG 可保存")
            return
        path = filedialog.asksaveasfilename(
            title="保存屏幕 SVG",
            defaultextension=".svg",
            initialfile="screen.svg",
            filetypes=[("SVG", "*.svg")],
        )
        if not path:
            return
        Path(path).write_text(self._svg_source, encoding="utf-8")
        self._status.set(f"已保存 SVG → {path}")

    def _save_png(self) -> None:
        session = self._registry.find(self._selected) if self._selected else None
        if session is None:
            return
        try:
            data = session.render_image(scale=_EXPORT_SCALE, fmt="png")
        except Exception as exc:
            messagebox.showinfo("保存 PNG", f"无屏幕位图: {exc}")
            return
        path = filedialog.asksaveasfilename(
            title="保存屏幕 PNG",
            defaultextension=".png",
            initialfile="screen.png",
            filetypes=[("PNG", "*.png")],
        )
        if not path:
            return
        Path(path).write_bytes(data)
        self._status.set(f"已保存 PNG → {path}")

    def _render_view(self, session: Session) -> str:
        if isinstance(session, TerminalSession):
            return self._screen_text(session)
        return self._render_streams(session)

    def _screen_text(self, session: Session) -> str:
        try:
            return session.screen_text()
        except Exception as exc:  # 宿主已关闭等
            return f"<屏幕不可用: {exc}>"

    @staticmethod
    def _render_streams(session: Session) -> str:
        parts = []
        for stream in session.streams():
            text = session.read_all(stream).decode("utf-8", errors="replace")
            parts.append(f"── {stream} ──\n{text}")
        return "\n".join(parts)

    def _render_raw(self, session: Session) -> str:
        """原始字节页：每路只取尾部——`read_all` 会把整个保留区复制一遍。"""
        lines = []
        for stream in session.streams():
            end = session.journal_for(stream).end_offset
            tail = session.read_range(max(0, end - _RAW_TAIL), end, stream)
            lines.append(f"── {stream} ──\n{tail!r}")
        return "\n".join(lines)

    @staticmethod
    def _processes(session: Session) -> tuple[int, ...] | None:
        """进程树成员；观测不到（未启动 / 没有作业对象 / 已关闭）返回 None。

        `descendants()` 是**轮询式**观测，这里每刷一次界面查一次；比对前后两次就能
        看出"谁起来了、谁没了"——判定留给命令层，核心层只出快照。
        """
        try:
            return session.descendants()
        except Exception:  # CoreError / MonitorUnavailable
            return None

    def _render_processes(self, session: Session) -> str:
        host = session.host
        root = host.pid if host is not None else None
        lines = [
            f"模式 {session.mode} · 状态 {session.state}"
            f" · 退出码 {'-' if session.exit_code is None else session.exit_code}",
            f"根进程 pid {root if root is not None else '-'}",
        ]
        members = self._processes(session)
        if members is None:
            lines.append("进程树成员：观测不到（未启动 / 没有作业对象 / 已关闭）")
            return "\n".join(lines)
        lines.append(f"进程树成员 {len(members)} 个（不含根进程）：")
        lines.extend(f"  pid {pid}" for pid in members)
        # 窗口探测只在 Windows 有实现；其他平台返回空并记一次告警
        wanted = {root} if root is not None else set()
        windows = windows_of(wanted | set(members))
        lines.append(f"可见窗口 {len(windows)} 个：")
        lines.extend(f'  pid {w.pid} · "{w.title}"' for w in windows)
        return "\n".join(lines)

    @staticmethod
    def _set_text(widget: tk.Text, text: str) -> None:
        if widget.get("1.0", "end-1c") == text:
            return
        at_bottom = widget.yview()[1] >= 0.999
        widget.delete("1.0", tk.END)
        widget.insert("1.0", text)
        if at_bottom:
            widget.see(tk.END)

    # ════════════════════════════════════════════════════════════
    # 收尾
    # ════════════════════════════════════════════════════════════

    def on_close(self) -> None:
        """窗口关闭：收尾所有会话（与守护进程退出的语义一致）。"""
        if self._tick_job is not None:  # 不取消的话，销毁后它还会触发一次并报错
            self._root.after_cancel(self._tick_job)
            self._tick_job = None
        self._registry.close_all()  # 会话收尾：先强杀进程树再关宿主
        for runner in list(self._runners.values()):
            runner.stop()
        self._runners.clear()
        self._root.destroy()


def main() -> int:
    root = tk.Tk()
    app = App(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()
    return 0
