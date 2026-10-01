"""示例层的 Tk 管理台：接入核心层接口，手动起会话、看屏幕、发输入。

    python -m agentic_tty.example --gui

**Tk 的 mainloop 就是所有者线程**：界面回调与 `SessionRunner.pump()` 都跑在同一
线程，所以这里不需要任何锁——这正是核心层"单线程所有者"约定带来的好处。
宿主的读由运行时层的读线程代劳（`SessionRunner`），界面线程从不阻塞。

模式三选一：`fake` 跑示例假程序（命令框下拉即假程序名）；`pty` / `subprocess`
跑真命令（命令框直接输入）。

右侧「屏幕」页显示 pty 会话的可见屏幕：`image` 格式走 `Session.render_image`
（终端模型直接出位图），`svg` 格式走 `Session.render_svg` 再经 resvg 栅格化——
**Tk 的 PhotoImage 只吃位图，没有 SVG 解码器**。「SVG 源码」页是同一块屏幕的矢量
源码。
"""

from __future__ import annotations

import base64
import shlex
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

try:
    import resvg_py
except ImportError as exc:  # 依赖缺失就说清楚怎么补，不静默降级
    raise ImportError("Tk 管理台渲染 SVG 需要 resvg-py：pip install -e .[gui]") from exc

from ..core.session.base import Session
from ..foundation.logs import get_logger
from ..runtime.runner import SessionRunner
from .programs import PROGRAMS
from .sessions import ExampleMode, create_session

_logger = get_logger("example.gui")

_TICK_MS = 20
# 每多少个 tick 刷一次界面（20ms × 8 ≈ 160ms，避免文本频繁重排）
_REFRESH_EVERY = 8
_RAW_TAIL = 2000
# 屏幕位图相对字符格基准（8×17 px）的缩放
_ZOOM = 2.0
_FORMAT_IMAGE = "image"
_FORMAT_SVG = "svg"


class App:
    """管理台。"""

    def __init__(self, root: tk.Tk) -> None:
        self._root = root
        self._sessions: dict[str, Session] = {}
        self._runners: dict[str, SessionRunner] = {}
        self._modes: dict[str, ExampleMode] = {}
        self._selected: str | None = None
        self._ticks = 0
        self._refreshing = False
        # 屏幕视图：屏幕没变（日志末尾没动）就不重渲染——位图渲染不便宜
        self._rendered_key: tuple[str, int, str] | None = None
        self._photo: tk.PhotoImage | None = None
        self._svg_source: str | None = None  # None = 该会话没有屏幕视图
        self._screen_note = ""  # 没有屏幕视图时的提示文字
        self._build_ui()
        self._root.after(_TICK_MS, self._tick)

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
        columns = ("command", "mode", "state", "exit")
        self._tree = ttk.Treeview(left, columns=columns, show="headings", selectmode="browse")
        for col, text, width in (
            ("command", "命令", 90),
            ("mode", "模式", 80),
            ("state", "状态", 70),
            ("exit", "退出码", 60),
        ):
            self._tree.heading(col, text=text)
            self._tree.column(col, width=width, anchor=tk.W, stretch=True)
        self._tree.pack(fill=tk.BOTH, expand=True)
        self._tree.bind("<<TreeviewSelect>>", self._on_select)

        right = ttk.Frame(body)
        body.add(right, weight=3)

        self._notebook = ttk.Notebook(right)
        self._notebook.pack(fill=tk.BOTH, expand=True)

        image_tab = ttk.Frame(self._notebook)
        format_row = ttk.Frame(image_tab)
        format_row.grid(row=0, column=0, columnspan=2, sticky="w", padx=6, pady=(4, 2))
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

        self._image_canvas = tk.Canvas(image_tab, highlightthickness=0)
        image_h = ttk.Scrollbar(image_tab, orient=tk.HORIZONTAL, command=self._image_canvas.xview)
        image_v = ttk.Scrollbar(image_tab, orient=tk.VERTICAL, command=self._image_canvas.yview)
        self._image_canvas.configure(xscrollcommand=image_h.set, yscrollcommand=image_v.set)
        self._image_canvas.grid(row=1, column=0, sticky="nsew")
        image_v.grid(row=1, column=1, sticky="ns")
        image_h.grid(row=2, column=0, sticky="ew")
        image_tab.rowconfigure(1, weight=1)
        image_tab.columnconfigure(0, weight=1)

        self._svg = tk.Text(self._notebook, wrap=tk.NONE, font=("Consolas", 9))
        self._view = tk.Text(self._notebook, wrap=tk.NONE, font=("Consolas", 10))
        self._raw = tk.Text(self._notebook, wrap=tk.NONE, font=("Consolas", 9))
        self._notebook.add(image_tab, text="屏幕")
        self._notebook.add(self._svg, text="SVG 源码")
        self._notebook.add(self._view, text="视图")
        self._notebook.add(self._raw, text="原始字节")

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
        ttk.Button(size_row, text="应用", command=self._resize).pack(side=tk.LEFT, padx=4)
        ttk.Button(size_row, text="保存 SVG", command=self._save_svg).pack(
            side=tk.LEFT, padx=(12, 0)
        )
        ttk.Button(size_row, text="保存 PNG", command=self._save_png).pack(side=tk.LEFT, padx=4)

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
            self._root.after(_TICK_MS, self._tick)

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
            session = create_session(mode, argv)
            session.start()
        except Exception as exc:
            messagebox.showerror("创建会话失败", str(exc))
            return
        runner = SessionRunner(session)
        runner.start()
        self._sessions[session.uid] = session
        self._runners[session.uid] = runner
        self._modes[session.uid] = mode
        self._selected = session.uid
        self._status.set(f"已创建 {' '.join(argv)}（{mode}）uid={session.uid[:8]}")
        self._focus_default_tab()
        self._refresh_tree()
        self._refresh_detail()

    def _close_selected(self) -> None:
        uid = self._selected
        session = self._sessions.pop(uid, None) if uid else None
        if session is None:
            return
        self._modes.pop(uid, None)
        session.close()  # 先强杀进程树再关宿主（核心层内部顺序）
        runner = self._runners.pop(uid, None)
        if runner is not None:
            runner.stop()
        self._selected = None
        self._status.set(f"已关闭 uid={uid[:8]}")
        self._refresh_tree()
        self._refresh_detail()

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
        session = self._sessions.get(self._selected) if self._selected else None
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
        if self._refreshing:
            return
        selection = self._tree.selection()
        self._selected = selection[0] if selection else None
        self._focus_default_tab()
        self._refresh_detail()

    def _focus_default_tab(self) -> None:
        """选中 pty 会话时默认停在「屏幕」页——它的屏幕视图就是位图。"""
        if self._modes.get(self._selected) is ExampleMode.PTY:
            self._notebook.select(0)

    # ════════════════════════════════════════════════════════════
    # 刷新
    # ════════════════════════════════════════════════════════════

    def _refresh_tree(self) -> None:
        self._refreshing = True
        try:
            for item in self._tree.get_children():
                self._tree.delete(item)
            for uid, session in self._sessions.items():
                self._tree.insert(
                    "",
                    tk.END,
                    iid=uid,
                    values=(
                        session.spec.argv[0] if session.spec.argv else "",
                        self._modes.get(uid, ""),
                        str(session.state),
                        "-" if session.exit_code is None else session.exit_code,
                    ),
                )
            if self._selected in self._sessions:
                self._tree.selection_set(self._selected)
        finally:
            self._refreshing = False

    def _refresh_detail(self) -> None:
        session = self._sessions.get(self._selected) if self._selected else None
        if session is None:
            self._set_text(self._view, "")
            self._set_text(self._raw, "")
            self._set_text(self._svg, "")
            self._svg_source = None
            self._screen_note = "未选中会话"
            self._set_image(None, self._screen_note)
            self._rendered_key = None
            return
        self._set_text(self._view, self._render_view(session))
        self._set_text(self._raw, self._render_raw(session))
        self._refresh_screen_views(session)
        drained = "已排空" if session.drained else "进行中"
        self._status.set(
            f"{session.spec.argv[0]} · {self._modes.get(session.uid, '')} · {session.state} · "
            f"{drained} · exit={session.exit_code} · uid={session.uid[:8]}"
        )

    def _refresh_screen_views(self, session: Session) -> None:
        """屏幕页 / SVG 源码页：屏幕或格式没变就跳过重渲染（渲染不便宜）。"""
        fmt = self._format.get()
        key = (session.uid, session.journal.end_offset, fmt)
        if key == self._rendered_key:
            return
        self._rendered_key = key
        try:
            self._svg_source = session.render_svg()
            self._screen_note = ""
        except Exception as exc:  # 子进程没有屏幕
            self._svg_source = None
            self._screen_note = f"<无屏幕视图: {exc}>"
        self._set_text(self._svg, self._svg_source or self._screen_note)
        self._set_image(*self._render_screen(session, fmt))

    def _render_screen(self, session: Session, fmt: str) -> tuple[bytes | None, str]:
        """屏幕页的位图：`image` 由终端模型直接出，`svg` 先出矢量再经 resvg 栅格化。"""
        if self._svg_source is None:  # 该会话根本没有屏幕
            return None, self._screen_note
        try:
            if fmt == _FORMAT_SVG:
                return resvg_py.svg_to_bytes(svg_string=self._svg_source, zoom=_ZOOM), ""
            return session.render_image(scale=_ZOOM, fmt="png"), ""
        except Exception as exc:  # 假宿主不渲染位图 / SVG 为空
            return None, f"<无屏幕位图: {exc}>"

    def _on_format_change(self) -> None:
        self._rendered_key = None  # 格式变了，强制重渲染
        self._refresh_detail()

    def _set_image(self, data: bytes | None, note: str) -> None:
        self._image_canvas.delete("all")
        self._photo = None  # 必须留引用，否则 Tk 会把图回收掉
        if data is None:
            self._image_canvas.create_text(12, 12, anchor="nw", text=note, fill="#888780")
            self._image_canvas.configure(scrollregion=(0, 0, 0, 0))
            return
        # Tk 的 PhotoImage 只吃位图（8.6 起原生支持 PNG）
        self._photo = tk.PhotoImage(data=base64.b64encode(data).decode("ascii"))
        self._image_canvas.create_image(0, 0, anchor="nw", image=self._photo)
        self._image_canvas.configure(scrollregion=(0, 0, self._photo.width(), self._photo.height()))

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
        session = self._sessions.get(self._selected) if self._selected else None
        if session is None:
            return
        try:
            data = session.render_image(scale=_ZOOM, fmt="png")
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
        mode = self._modes.get(session.uid)
        if mode is ExampleMode.SUBPROCESS:
            return self._render_streams(session)
        if mode is ExampleMode.FAKE:
            # 假会话同时有屏幕与双流：三段都给出，便于对照
            return "\n".join(
                (f"── 屏幕 ──\n{self._screen_text(session)}", self._render_streams(session))
            )
        return self._screen_text(session)

    def _screen_text(self, session: Session) -> str:
        # 文本视图还没进端口（要等命令层"返回数据"过滤的设计）：真宿主有
        # screen_text，假宿主退回纯文本尾部。
        screen = getattr(session.host, "screen_text", None)
        if callable(screen):
            try:
                return screen()
            except Exception as exc:  # 宿主已关闭等：退回 snapshot
                _logger.debug("screen_text 不可用 uid=%s: %s", session.uid, exc)
        try:
            return session.snapshot().decode("utf-8", errors="replace")
        except Exception as exc:
            return f"<屏幕不可用: {exc}>"

    @staticmethod
    def _render_streams(session: Session) -> str:
        parts = []
        for stream in session.streams():
            text = session.read_all(stream).decode("utf-8", errors="replace")
            parts.append(f"── {stream} ──\n{text}")
        return "\n".join(parts)

    def _render_raw(self, session: Session) -> str:
        lines = []
        for stream in session.streams():
            tail = session.read_all(stream)[-_RAW_TAIL:]
            lines.append(f"── {stream} ──\n{tail!r}")
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
        for uid, session in list(self._sessions.items()):
            try:
                session.close()
            except Exception:
                _logger.exception("关闭会话异常 uid=%s", uid)
            runner = self._runners.get(uid)
            if runner is not None:
                runner.stop()
        self._sessions.clear()
        self._runners.clear()
        self._modes.clear()
        self._root.destroy()


def main() -> int:
    root = tk.Tk()
    app = App(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()
    return 0
