"""示例层的 Tk 管理台：接入核心层接口，手动起会话、看屏幕、发输入。

    python -m agentic_tty.example --gui

**Tk 的 mainloop 就是所有者线程**：界面回调与 `SessionRunner.pump()` 都跑在同一
线程，所以这里不需要任何锁——这正是核心层"单线程所有者"约定带来的好处。
宿主的读由运行时层的读线程代劳（`SessionRunner`），界面线程从不阻塞。

命令框是**可编辑的**：下拉里是示例假程序（`build` / `noisy` / `repl` / `tail` /
`crash`，不启动真程序）；直接输入真命令则走运行时层的真宿主。
"""

from __future__ import annotations

import shlex
import tkinter as tk
from tkinter import messagebox, ttk

from ..core.ports import SessionMode, SessionSpec, Stream
from ..core.session.base import Session
from ..core.session.registry import SessionRegistry
from ..foundation.logs import get_logger
from ..runtime.runner import SessionRunner
from .hosts import make_host
from .programs import PROGRAMS

_logger = get_logger("example.gui")

_TICK_MS = 20
# 每多少个 tick 刷一次界面（20ms × 8 ≈ 160ms，避免文本频繁重排）
_REFRESH_EVERY = 8
_RAW_TAIL = 2000


class App:
    """管理台。"""

    def __init__(self, root: tk.Tk) -> None:
        self._root = root
        self._registry = SessionRegistry(host_factory=make_host, journal_budget_bytes=1 << 20)
        self._sessions: dict[str, Session] = {}
        self._runners: dict[str, SessionRunner] = {}
        self._selected: str | None = None
        self._ticks = 0
        self._refreshing = False
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
        self._mode = tk.StringVar(value=SessionMode.PTY.value)
        for text, value in (
            ("pty", SessionMode.PTY.value),
            ("subprocess", SessionMode.PROCESS.value),
        ):
            ttk.Radiobutton(top, text=text, value=value, variable=self._mode).pack(
                side=tk.LEFT, padx=(4, 0)
            )
        ttk.Label(top, text="   命令").pack(side=tk.LEFT)
        self._command = ttk.Combobox(top, values=sorted(PROGRAMS), width=18)
        self._command.set("repl")
        self._command.pack(side=tk.LEFT)
        ttk.Button(top, text="创建会话", command=self._create_session).pack(side=tk.LEFT, padx=6)
        ttk.Button(top, text="关闭选中", command=self._close_selected).pack(side=tk.LEFT)
        ttk.Label(
            top,
            text="（下拉是示例假程序；直接输入真命令则走真宿主）",
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
        self._view = tk.Text(self._notebook, wrap=tk.NONE, font=("Consolas", 10))
        self._raw = tk.Text(self._notebook, wrap=tk.NONE, font=("Consolas", 9))
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

    def _create_session(self) -> None:
        text = self._command.get().strip()
        if not text:
            messagebox.showwarning("创建会话", "请填命令")
            return
        mode = SessionMode(self._mode.get())
        # 命令按 shell 语义拆分，支持 "cmd.exe /c dir" 这种整串
        argv = tuple(shlex.split(text)) or (text,)
        try:
            session = self._registry.create(SessionSpec(mode=mode, argv=argv))
            session.start()
        except Exception as exc:
            messagebox.showerror("创建会话失败", str(exc))
            return
        runner = SessionRunner(session)
        runner.start()
        self._sessions[session.uid] = session
        self._runners[session.uid] = runner
        self._selected = session.uid
        self._status.set(f"已创建 {' '.join(argv)}（{mode}）uid={session.uid[:8]}")
        self._refresh_tree()
        self._refresh_detail()

    def _close_selected(self) -> None:
        uid = self._selected
        session = self._sessions.get(uid) if uid else None
        runner = self._runners.pop(uid, None) if uid else None
        if session is None:
            return
        self._sessions.pop(uid, None)
        session.close()  # 先强杀进程树再关宿主（核心层内部顺序）
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
        self._refresh_detail()

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
                        str(session.mode),
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
            return
        self._set_text(self._view, self._render_view(session))
        self._set_text(self._raw, self._render_raw(session))
        drained = "已排空" if session.drained else "进行中"
        self._status.set(
            f"{session.spec.argv[0]} · {session.mode} · {session.state} · {drained} · "
            f"exit={session.exit_code} · uid={session.uid[:8]}"
        )

    def _render_view(self, session: Session) -> str:
        if session.mode is SessionMode.PTY:
            # 真宿主能直接给可见屏幕纯文本；假宿主没有，退回 snapshot
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
        parts = [
            f"── stdout ──\n{session.read_all(Stream.STDOUT).decode('utf-8', errors='replace')}"
        ]
        parts.append(
            f"── stderr ──\n{session.read_all(Stream.STDERR).decode('utf-8', errors='replace')}"
        )
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
        self._root.destroy()


def main() -> int:
    root = tk.Tk()
    app = App(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()
    return 0
