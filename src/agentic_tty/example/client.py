"""示例客户端：一个连到守护进程的 Tk 界面。

    python -m agentic_tty.example.client [--uri tcp://127.0.0.1:8765]

和 `example.gui` 那个管理台正好成对照——**管理台在进程内直连核心层，这个客户端只走网线**。
它因此是这个工程里"客户端链"的活证明：只依赖 `foundation + protocol + transport`，
`service` / `core` / `runtime` 一概够不着（有分层测试拦着）。

看不到终端模型、也渲染不出屏幕，所以屏幕位图只能由守护进程渲染好、按字节帧送过来。

I/O 全在 UI 线程上用零超时做：请求很小，守护进程每 5ms 就排空一次入站队列，触发不了背压；
真要跨机或上大流量，这些 I/O 得挪进后台线程。
"""

from __future__ import annotations

import argparse
import base64
import sys
import tkinter as tk
from collections.abc import Sequence
from tkinter import messagebox, ttk

from ..foundation.logs import configure, get_logger
from ..foundation.paths import default_runtime_dir
from ..protocol.envelope import Envelope, make_request
from ..protocol.frame import BytesFrame, ControlFrame
from ..protocol.messages import (
    DEFAULT_CELL_HEIGHT,
    DEFAULT_CELL_WIDTH,
    Kind,
    data_of,
    error_of,
)
from ..transport import registry as transports
from ..transport.channel import Channel, decode_control
from ..transport.errors import ConnectionClosed, TransportError

_logger = get_logger("example.client")

_TICK_MS = 20
_REFRESH_EVERY = 10  # 20ms × 10 ≈ 200ms 拉一次视图
_LIST_EVERY = 25  # ≈500ms 刷一次会话列表
_RAW_TAIL = 4000  # 原始字节页只取最后这么多字节
_FULL_TAIL_LINES = 500  # 全量输出页只取最后这么多行
_CONNECT_TIMEOUT = 2.0

_TAB_IMAGE = "屏幕"
_TAB_SCREEN = "屏幕文本"
_TAB_FULL = "全量输出"
_TAB_RAW = "原始字节"


def _endpoint_uri(name: str) -> str:
    """从运行时目录的端点文件里读守护进程地址。"""
    path = default_runtime_dir(name) / "endpoint"
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


class ClientApp:
    """客户端界面。"""

    def __init__(self, root: tk.Tk, *, uri: str, name: str) -> None:
        self._root = root
        self._name = name
        self._channel: Channel | None = None
        self._waiting: dict[str, str] = {}
        self._binary_tag: dict[str, str] = {}
        self._sessions: dict[str, dict] = {}
        self._selected: str | None = None
        self._pending_select: str | None = None
        self._sid_seq = 0
        self._ticks = 0
        self._photo: tk.PhotoImage | None = None
        self._tick_job: str | None = None
        self._build_ui(uri)
        self._tick_job = self._root.after(_TICK_MS, self._tick)

    # ════════════════════════════════════════════════════════════
    # 界面
    # ════════════════════════════════════════════════════════════

    def _build_ui(self, uri: str) -> None:
        self._root.title("agentic-tty · 示例客户端（走网线）")
        self._root.geometry("1060x680")

        top = ttk.Frame(self._root, padding=(8, 6))
        top.pack(fill=tk.X)
        ttk.Label(top, text="守护进程").pack(side=tk.LEFT)
        self._uri = tk.StringVar(value=uri)
        ttk.Entry(top, textvariable=self._uri, width=30).pack(side=tk.LEFT, padx=(4, 0))
        self._connect_btn = ttk.Button(top, text="连接", command=self._connect)
        self._connect_btn.pack(side=tk.LEFT, padx=6)
        self._disconnect_btn = ttk.Button(
            top, text="断开", command=self._disconnect, state="disabled"
        )
        self._disconnect_btn.pack(side=tk.LEFT)
        ttk.Label(
            top,
            text="（留空则读运行时目录的端点文件；先跑 python -m agentic_tty.example.daemon）",
            foreground="#888780",
        ).pack(side=tk.LEFT, padx=8)

        body = ttk.Panedwindow(self._root, orient=tk.HORIZONTAL)
        body.pack(fill=tk.BOTH, expand=True, padx=8)

        left = ttk.Frame(body)
        body.add(left, weight=1)
        self._tree = ttk.Treeview(
            left,
            columns=("sid", "mode", "state", "exit"),
            show="headings",
            selectmode="browse",
        )
        for col, text, width in (
            ("sid", "sid", 90),
            ("mode", "模式", 60),
            ("state", "状态", 70),
            ("exit", "退出码", 55),
        ):
            self._tree.heading(col, text=text)
            self._tree.column(col, width=width, anchor=tk.W, stretch=True)
        self._tree.pack(fill=tk.BOTH, expand=True)
        self._tree.bind("<<TreeviewSelect>>", self._on_select)

        create_row = ttk.Frame(left)
        create_row.pack(fill=tk.X, pady=(6, 0))
        self._command = ttk.Entry(create_row)
        self._command.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(create_row, text="新建", command=self._create).pack(side=tk.LEFT, padx=4)
        ttk.Button(create_row, text="关闭选中", command=self._remove_selected).pack(side=tk.LEFT)
        ttk.Label(left, text="留空 = 起 shell；填了就当命令跑", foreground="#888780").pack(
            anchor=tk.W, pady=(2, 0)
        )

        right = ttk.Frame(body)
        body.add(right, weight=3)
        self._notebook = ttk.Notebook(right)
        self._notebook.pack(fill=tk.BOTH, expand=True)

        self._image_tab = ttk.Frame(self._notebook)
        self._canvas = tk.Canvas(self._image_tab, highlightthickness=0)
        self._canvas.pack(fill=tk.BOTH, expand=True)
        self._notebook.add(self._image_tab, text=_TAB_IMAGE)
        self._texts: dict[str, tk.Text] = {}
        for name in (_TAB_SCREEN, _TAB_FULL, _TAB_RAW):
            widget = tk.Text(self._notebook, wrap=tk.NONE, font=("Consolas", 10))
            self._notebook.add(widget, text=name)
            self._texts[name] = widget
        self._notebook.bind("<<NotebookTabChanged>>", lambda _e: self._refresh())

        entry_row = ttk.Frame(right)
        entry_row.pack(fill=tk.X, pady=(6, 0))
        self._input = ttk.Entry(entry_row)
        self._input.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self._input.bind("<Return>", lambda _e: self._send_text(newline=True))
        ttk.Button(entry_row, text="发送", command=self._send_text).pack(side=tk.LEFT, padx=4)
        ttk.Button(entry_row, text="发送+换行", command=lambda: self._send_text(newline=True)).pack(
            side=tk.LEFT
        )
        ttk.Button(entry_row, text="Ctrl+C", command=lambda: self._send_raw(b"\x03")).pack(
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
        ttk.Button(size_row, text="改尺寸", command=self._resize).pack(side=tk.LEFT, padx=4)

        self._status = tk.StringVar(value="未连接")
        ttk.Label(self._root, textvariable=self._status, relief=tk.SUNKEN, anchor=tk.W).pack(
            fill=tk.X
        )
        self._sync_controls()

    def _sync_controls(self) -> None:
        connected = self._channel is not None
        self._connect_btn.state(["disabled"] if connected else ["!disabled"])
        self._disconnect_btn.state(["!disabled"] if connected else ["disabled"])

    # ════════════════════════════════════════════════════════════
    # 连接
    # ════════════════════════════════════════════════════════════

    def _connect(self) -> None:
        uri = self._uri.get().strip() or _endpoint_uri(self._name)
        if not uri:
            messagebox.showwarning("连接", "没填地址，也没找到端点文件——先起守护进程")
            return
        try:
            self._channel = Channel(transports.connect(uri, timeout=_CONNECT_TIMEOUT))
        except (TransportError, OSError) as exc:
            self._channel = None
            messagebox.showerror("连接失败", str(exc))
            return
        self._uri.set(uri)
        self._status.set(f"已连接 {uri}")
        self._sync_controls()
        self._refresh()

    def _disconnect(self) -> None:
        if self._channel is not None:
            self._channel.close()
        self._channel = None
        self._waiting.clear()
        self._binary_tag.clear()
        self._sessions.clear()
        self._selected = None
        for item in self._tree.get_children():
            self._tree.delete(item)
        self._photo = None
        self._canvas.delete("all")
        self._status.set("已断开")
        self._sync_controls()

    # ════════════════════════════════════════════════════════════
    # 驱动循环
    # ════════════════════════════════════════════════════════════

    def _tick(self) -> None:
        try:
            self._pump()
            self._ticks += 1
            if self._channel is not None and self._ticks % _REFRESH_EVERY == 0:
                self._refresh()
            if self._channel is not None and self._ticks % _LIST_EVERY == 0:
                self._send("list_sessions", "sessions")
        except Exception:
            _logger.exception("客户端驱动循环异常")
        finally:
            self._tick_job = self._root.after(_TICK_MS, self._tick)

    def _pump(self) -> None:
        channel = self._channel
        if channel is None:
            return
        try:
            frames = channel.recv(timeout=0)
        except ConnectionClosed as exc:
            self._disconnect()
            self._status.set(f"连接已断开: {exc}")
            return
        for frame in frames:
            if isinstance(frame, ControlFrame):
                self._on_reply(decode_control(frame))
            elif isinstance(frame, BytesFrame):
                self._on_binary(frame)

    def _send(self, type_: str, tag: str, **op: object) -> None:
        channel = self._channel
        if channel is None:
            return
        request = make_request(type_, op=op)
        self._waiting[request.mid] = tag
        try:
            channel.send(request)
        except ConnectionClosed as exc:
            self._disconnect()
            self._status.set(f"连接已断开: {exc}")

    def _on_reply(self, envelope: Envelope) -> None:
        tag = self._waiting.pop(envelope.mid, None)
        failure = error_of(envelope)
        if failure is not None:
            self._status.set(f"{envelope.type} 失败: {failure.code} · {failure.message}")
            return
        data = data_of(envelope)
        if tag == "status":
            self._status.set(
                f"守护进程 pid={data.get('pid')} 会话 {data.get('sessions')} "
                f"运行 {data.get('uptime')}s listen={data.get('listen')}"
            )
        elif tag == "sessions":
            self._apply_sessions(data.get("sessions") or [])
        elif tag == "read" and envelope.kind in (Kind.IMAGE.value, Kind.BYTES.value):
            self._binary_tag[envelope.mid] = envelope.kind  # 数据在紧随其后的字节帧里
        elif tag == "read":
            self._show_text(envelope.kind, data.get("text") or "")
        elif tag == "create":
            self._selected = str(data.get("sid") or self._pending_select or "")
            self._pending_select = None
            self._status.set(f"已创建 {self._selected}")
            self._send("list_sessions", "sessions")
        elif tag is not None:
            self._status.set(f"{envelope.type} 完成")

    def _on_binary(self, frame: BytesFrame) -> None:
        kind = self._binary_tag.pop(frame.key, None)
        if kind == Kind.IMAGE.value:
            self._show_image(frame.data)
        elif kind == Kind.BYTES.value:
            self._show_text(Kind.BYTES.value, frame.data.decode("utf-8", errors="replace"))
        else:
            self._status.set(f"收到 {len(frame.data)} 字节原始输出（未关联到请求）")

    # ════════════════════════════════════════════════════════════
    # 刷新
    # ════════════════════════════════════════════════════════════

    def _refresh(self) -> None:
        sid = self._selected
        if self._channel is None or sid is None or sid not in self._sessions:
            return
        tab = self._notebook.tab(self._notebook.select(), "text")
        if tab == _TAB_IMAGE:
            self._send("read_terminal", "read", sid=sid, mode="image", scale=self._fit_scale(sid))
        elif tab == _TAB_SCREEN:
            self._send("read_terminal", "read", sid=sid, mode="screen")
        elif tab == _TAB_FULL:
            # 只取尾巴：全量输出可能到日志预算（默认 1MB），每 200ms 整段搬过来重画会把界面拖死。
            self._send("read_terminal", "read", sid=sid, mode="text", lines=_FULL_TAIL_LINES)
        else:
            self._send("read_terminal", "read", sid=sid, mode="bytes", tail=_RAW_TAIL)

    def _fit_scale(self, sid: str) -> float:
        info = self._sessions.get(sid) or {}
        cols, rows = info.get("cols") or 80, info.get("rows") or 24
        width, height = self._canvas.winfo_width(), self._canvas.winfo_height()
        if width <= 1 or height <= 1:
            return 1.0
        return max(
            0.2,
            min(
                width / (cols * DEFAULT_CELL_WIDTH),
                height / (rows * DEFAULT_CELL_HEIGHT),
            ),
        )

    def _apply_sessions(self, rows: list[dict]) -> None:
        """增量刷新会话表：行 iid 就是 sid。

        **不能全表重建**：`<<TreeviewSelect>>` 是异步派发的，删空表会让它带着空选中
        跑一次 `_on_select`，把当前选中的会话弄丢（管理台那边踩过同一个坑）。
        """
        self._sessions = {str(row.get("sid")): row for row in rows}
        alive = set(self._sessions)
        for item in self._tree.get_children():
            if item not in alive:
                self._tree.delete(item)
        for sid, row in self._sessions.items():
            values = (sid, row.get("mode"), row.get("state"), row.get("exit_code"))
            if not self._tree.exists(sid):
                self._tree.insert("", tk.END, iid=sid, values=values)
            elif self._tree.item(sid, "values") != tuple(str(v) for v in values):
                self._tree.item(sid, values=values)
        if self._selected in alive and self._selected not in self._tree.selection():
            self._tree.selection_set(self._selected)

    def _on_select(self, _event: object = None) -> None:
        selection = self._tree.selection()
        sid = selection[0] if selection else None
        if sid == self._selected:
            return
        self._selected = sid
        self._refresh()

    def _show_text(self, kind: str, text: str) -> None:
        if kind == Kind.TEXT.value:
            target = _TAB_FULL
        elif kind == Kind.BYTES.value:
            target = _TAB_RAW
        else:
            target = _TAB_SCREEN
        widget = self._texts[target]
        if widget.get("1.0", "end-1c") == text:
            return
        widget.delete("1.0", tk.END)
        widget.insert("1.0", text)

    def _show_image(self, blob: bytes) -> None:
        try:
            self._photo = tk.PhotoImage(data=base64.b64encode(blob).decode("ascii"))
        except tk.TclError as exc:  # 位图坏了不该把界面搞崩
            self._status.set(f"屏幕位图无法显示: {exc}")
            return
        self._canvas.delete("all")
        self._canvas.create_image(
            self._canvas.winfo_width() / 2,
            self._canvas.winfo_height() / 2,
            image=self._photo,
        )

    # ════════════════════════════════════════════════════════════
    # 操作
    # ════════════════════════════════════════════════════════════

    def _create(self) -> None:
        if self._channel is None:
            messagebox.showwarning("新建会话", "先连上守护进程")
            return
        self._sid_seq += 1
        sid = f"sh-{self._sid_seq}"
        command = self._command.get().strip()
        self._pending_select = sid
        if command:
            self._send("create_terminal", "create", sid=sid, command=command)
        else:
            self._send("create_shell_terminal", "create", sid=sid)

    def _remove_selected(self) -> None:
        if self._selected:
            self._send("remove_session", "remove", sids=[self._selected])

    def _send_text(self, newline: bool = False) -> None:
        sid = self._selected
        if sid is None or self._channel is None:
            return
        text = self._input.get() + ("\n" if newline else "")
        self._send("input_into_terminal", "input", sid=sid, text=text)
        self._input.delete(0, tk.END)

    def _send_raw(self, data: bytes) -> None:
        """原始字节直接走字节帧——**字节帧本身就是一次操作**，不需要配对的控制帧。"""
        if self._selected is None or self._channel is None:
            return
        try:
            self._channel.send_bytes("stdout", self._selected, data)
        except ConnectionClosed as exc:
            self._disconnect()
            self._status.set(f"连接已断开: {exc}")

    def _resize(self) -> None:
        sid = self._selected
        if sid is None:
            return
        try:
            cols, rows = int(self._cols.get()), int(self._rows.get())
        except ValueError:
            messagebox.showwarning("改尺寸", "宽高要是整数")
            return
        self._send("resize_terminal", "resize", sid=sid, cols=cols, rows=rows)

    # ════════════════════════════════════════════════════════════
    # 收尾
    # ════════════════════════════════════════════════════════════

    def on_close(self) -> None:
        """关窗：只是断开连接。**会话照常活着**——这正是这套设计要演示的东西。"""
        if self._tick_job is not None:
            self._root.after_cancel(self._tick_job)
            self._tick_job = None
        self._disconnect()
        self._root.destroy()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agentic_tty.example.client", description="示例客户端")
    parser.add_argument("--uri", default="", help="守护进程地址；留空则读端点文件")
    parser.add_argument("--name", default="agentic-tty-example", help="运行时目录名")
    args = parser.parse_args(argv)

    configure()
    root = tk.Tk()
    app = ClientApp(root, uri=args.uri or _endpoint_uri(args.name), name=args.name)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
