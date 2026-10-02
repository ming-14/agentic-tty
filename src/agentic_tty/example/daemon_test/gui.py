"""守护进程的验证台：界面与 `core_test/gui.py` 同款，差别只在**挂载点**。

- `core_test/gui.py`：直接驱动核心层（自己装 core + runtime），不套守护进程。
- 本文件：把核心层交给守护进程承载，**一切数据都经守护进程的接缝**走一遍。

因此每个动作都是一次请求：`submit()` 投进去，答复由 `on_reply` 回调回来，界面在 tick
里取出后更新。`on_reply` **跑在所有者线程上**，所以它只入队，不碰界面。

**它只碰守护进程的接缝**：`start` / `request_stop` / `stop` / `running` / `pid_path` /
`runtime_dir` / `submit` / `submit_input` / `on_reply`。核心层由本包 `handler.py` 驱动。
"""

from __future__ import annotations

import base64
import re
import shlex
import threading
import tkinter as tk
from pathlib import Path
from queue import Empty, Queue
from tkinter import filedialog, messagebox, ttk

try:
    import resvg_py
except ImportError as exc:  # 依赖缺失就说清楚怎么补，不静默降级
    raise ImportError("验证台渲染 SVG 需要 resvg-py：pip install -e .[gui]") from exc

from ...daemon.config import DaemonConfig
from ...daemon.handler import Reply
from ...daemon.server import Daemon
from ...foundation.logs import get_logger
from ...foundation.paths import default_runtime_dir
from .handler import Answer, KernelHandler, Request

_logger = get_logger("example.daemon_test.gui")

_TICK_MS = 20
# 每多少个 tick 刷一次界面（20ms × 8 ≈ 160ms）。刷新 = 两次请求往返。
_REFRESH_EVERY = 8
# 保存 PNG 用的固定缩放（屏幕页显示时会按画布大小另算）
_EXPORT_SCALE = 2.0
_FORMAT_IMAGE = "image"
_FORMAT_SVG = "svg"
_STOP_TIMEOUT = 2.0
_NAME = "agentic-tty-daemon-test"


def _svg_size(svg: str) -> tuple[int, int] | None:
    """渲染结果自带的像素尺寸（1.0 倍）——根元素上写着 width / height。"""
    root = svg.split(">", 1)[0]
    width = re.search(r'\bwidth="([\d.]+)"', root)
    height = re.search(r'\bheight="([\d.]+)"', root)
    if width is None or height is None:
        return None
    return int(float(width.group(1))), int(float(height.group(1)))


class App:
    """验证台。"""

    def __init__(self, root: tk.Tk) -> None:
        self._root = root
        self._config = DaemonConfig(name=_NAME, runtime_dir=default_runtime_dir(_NAME))
        self._handler = KernelHandler()
        self._replies: Queue[Reply] = Queue()
        self._daemon = Daemon(self._config, lambda: self._handler, on_reply=self._on_reply)
        self._thread: threading.Thread | None = None

        self._ticks = 0
        self._sid_seq = 0
        self._selected: str | None = None
        self._sessions: dict[str, dict] = {}
        self._detail: dict | None = None
        # 请求 → 用途：答复回来时靠请求对象的身份认出它属于哪一次询问
        self._want: dict[int, str] = {}
        self._export_path: str | None = None

        # 屏幕视图：会话 / 日志偏移 / 格式 / 画布尺寸没变就不重渲染——栅格化不便宜
        self._rendered_key: tuple[str, int, str, float] | None = None
        self._photo: tk.PhotoImage | None = None
        self._svg_source: str | None = None
        self._screen_note = ""

        self._tick_job: str | None = None
        self._build_ui()
        self._sync_pty_controls(None)
        self._tick_job = self._root.after(_TICK_MS, self._tick)

    # ════════════════════════════════════════════════════════════
    # 接缝回调（跑在所有者线程上）
    # ════════════════════════════════════════════════════════════

    def _on_reply(self, reply: Reply) -> None:
        """守护进程把答复交回来。**在所有者线程上被调用**——只入队，不碰界面。"""
        self._replies.put(reply)

    # ════════════════════════════════════════════════════════════
    # 界面
    # ════════════════════════════════════════════════════════════

    def _build_ui(self) -> None:
        self._root.title("agentic-tty · 守护进程验证台")
        self._root.geometry("1100x700")

        daemon_row = ttk.Frame(self._root, padding=(8, 6))
        daemon_row.pack(fill=tk.X)
        ttk.Label(daemon_row, text="守护进程").pack(side=tk.LEFT)
        self._daemon_state = tk.StringVar(value="未启动")
        ttk.Label(daemon_row, textvariable=self._daemon_state).pack(side=tk.LEFT, padx=8)
        for text, command in (
            ("启动", self._start),
            ("请求停止", self._request_stop),
            (f"stop({_STOP_TIMEOUT}s)", self._stop),
            ("再起一个（试单实例）", self._start_second),
        ):
            ttk.Button(daemon_row, text=text, command=command).pack(side=tk.LEFT, padx=2)
        ttk.Label(
            daemon_row, text=f"运行时目录 {self._config.runtime_dir}", foreground="#888780"
        ).pack(side=tk.LEFT, padx=8)

        top = ttk.Frame(self._root, padding=(8, 0))
        top.pack(fill=tk.X)
        ttk.Label(top, text="模式").pack(side=tk.LEFT)
        self._mode = tk.StringVar(value="pty")
        for value in ("pty", "subprocess"):
            ttk.Radiobutton(top, text=value, value=value, variable=self._mode).pack(
                side=tk.LEFT, padx=(4, 0)
            )
        ttk.Label(top, text="   命令").pack(side=tk.LEFT)
        self._command = ttk.Entry(top, width=32)
        self._command.pack(side=tk.LEFT)
        ttk.Button(top, text="创建会话", command=self._create_session).pack(side=tk.LEFT, padx=6)
        ttk.Button(top, text="关闭选中", command=self._close_selected).pack(side=tk.LEFT)
        ttk.Label(top, text="（留空 = 平台默认 shell）", foreground="#888780").pack(
            side=tk.LEFT, padx=6
        )

        body = ttk.Panedwindow(self._root, orient=tk.HORIZONTAL)
        body.pack(fill=tk.BOTH, expand=True, padx=8, pady=(4, 6))

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
    # 驱动循环
    # ════════════════════════════════════════════════════════════

    def _tick(self) -> None:
        try:
            self._drain()
            self._ticks += 1
            if self._ticks % _REFRESH_EVERY == 0 and self._daemon.running:
                self._ask(Request("list"), "list")
                if self._selected is not None:
                    self._ask(Request("detail", sid=self._selected), "detail")
            self._refresh_daemon_state()
        except Exception:
            _logger.exception("驱动循环异常")
        finally:
            self._tick_job = self._root.after(_TICK_MS, self._tick)

    def _drain(self) -> None:
        while True:
            try:
                reply = self._replies.get_nowait()
            except Empty:
                return
            self._dispatch_reply(reply)

    # ════════════════════════════════════════════════════════════
    # 请求 / 答复
    # ════════════════════════════════════════════════════════════

    def _ask(self, request: Request, purpose: str) -> None:
        """把一条请求投给守护进程；答复回来时按 `purpose` 处理。"""
        if self._thread is None:
            self._status.set("守护进程没在跑——先点「启动」")
            return
        self._want[id(request)] = purpose
        if not self._daemon.submit(request):
            self._want.pop(id(request), None)
            self._status.set("守护进程已在收尾，这条请求被放弃了")

    def _dispatch_reply(self, reply: Reply) -> None:
        purpose = self._want.pop(id(reply.request), None)
        answer = reply.answer
        if not isinstance(answer, Answer):
            self._status.set(f"答复类型不对: {type(answer)!r}")
            return
        if not answer.ok:
            self._status.set(f"失败（{purpose}）: {answer.error}")
            return
        if purpose == "list":
            self._apply_sessions(answer.data.get("sessions") or [])
        elif purpose == "detail":
            self._apply_detail(answer.data)
        elif purpose == "image":
            self._show_image(answer.data.get("blob"))
        elif purpose == "export":
            self._write_export(answer.data.get("blob"))
        elif purpose == "create":
            self._selected = str(answer.data.get("sid") or "")
            self._status.set(f"已创建 {self._selected}")
        elif purpose == "resize":
            self._status.set(f"尺寸已改为 {answer.data.get('cols')}×{answer.data.get('rows')}")
        elif purpose == "close":
            self._select_session(None)

    def _apply_sessions(self, rows: list[dict]) -> None:
        """增量刷新会话表：行 iid 就是 sid。

        不能全表重建 + `selection_set`：`<<TreeviewSelect>>` 是异步派发的，删空表会让它
        带着空选中跑一次，把当前选中的会话弄丢。
        """
        self._sessions = {str(row.get("sid")): row for row in rows}
        alive = set(self._sessions)
        for item in self._tree.get_children():
            if item not in alive:
                self._tree.delete(item)
        for sid, row in self._sessions.items():
            values = tuple(str(row.get(key) or "-") for key in _ROWS)
            if not self._tree.exists(sid):
                self._tree.insert("", tk.END, iid=sid, values=values)
            elif self._tree.item(sid, "values") != values:
                self._tree.item(sid, values=values)
        if self._selected in alive and self._selected not in self._tree.selection():
            self._tree.selection_set(self._selected)

    def _apply_detail(self, data: dict) -> None:
        self._detail = data
        self._set_text(self._view, str(data.get("view") or ""))
        self._set_text(self._raw, str(data.get("raw") or ""))
        self._set_text(self._procs, str(data.get("procs") or ""))
        self._sync_pty_controls(data)
        self._refresh_screen_view(data)
        drained = "已排空" if data.get("drained") else "进行中"
        self._status.set(
            f"{data.get('command')} · {data.get('mode')} · {data.get('state')} · {drained}"
            f" · exit={data.get('exit_code')}"
        )

    def _refresh_screen_view(self, data: dict) -> None:
        """屏幕页 / SVG 源码页（pty 专属）：先出 SVG，再从渲染结果自己读 1.0 倍尺寸。

        屏幕 / 日志偏移 / 格式 / 画布尺寸都没变就跳过——贵的是栅格化那一趟。
        """
        sid = str(data.get("sid") or "")
        svg = data.get("svg")
        if not data.get("is_terminal") or svg is None:
            self._rendered_key = None
            self._svg_source = None
            note = self._screen_note_for(data)
            self._set_text(self._svg, note)
            self._set_image(None, note)
            return
        size = _svg_size(str(svg))
        if size is None:
            self._rendered_key = None
            self._svg_source = str(svg)
            self._set_text(self._svg, self._svg_source)
            self._set_image(None, "<渲染结果里没有尺寸，无法铺满画布>")
            return
        fmt = self._format.get()
        scale = self._fit_scale(size)
        key = (sid, int(data.get("offset") or 0), fmt, round(scale, 4))
        if key == self._rendered_key:
            return
        self._rendered_key = key
        self._svg_source = str(svg)
        self._set_text(self._svg, self._svg_source)
        if fmt == _FORMAT_SVG:
            self._set_image(*self._rasterize(self._svg_source, scale))
        else:
            self._ask(Request("image", sid=sid, scale=scale), "image")

    @staticmethod
    def _screen_note_for(data: dict) -> str:
        if not data.get("is_terminal"):
            return "该会话没有屏幕（只有子进程会话才有字节流）"
        return f"<无屏幕视图: {data.get('svg_error') or '宿主不可用'}>"

    def _fit_scale(self, size: tuple[int, int]) -> float:
        """让屏幕正好铺满画布（不出现滚动条）。画布还没量出尺寸时按 1× 画。"""
        base_width, base_height = size
        if base_width <= 0 or base_height <= 0:
            return 1.0
        width, height = self._image_canvas.winfo_width(), self._image_canvas.winfo_height()
        if width <= 1 or height <= 1:
            return 1.0
        return min(width / base_width, height / base_height)

    @staticmethod
    def _rasterize(svg: str, scale: float) -> tuple[bytes | None, str]:
        try:
            return resvg_py.svg_to_bytes(svg_string=svg, zoom=scale), ""
        except Exception as exc:  # SVG 为空
            return None, f"<无屏幕位图: {exc}>"

    def _on_format_change(self) -> None:
        self._rendered_key = None  # 格式变了，强制重渲染
        if self._selected is not None:
            self._ask(Request("detail", sid=self._selected), "detail")

    # ════════════════════════════════════════════════════════════
    # 显示
    # ════════════════════════════════════════════════════════════

    def _show_image(self, blob: object) -> None:
        self._set_image(blob if isinstance(blob, bytes) else None, "<位图不可用>")

    def _set_image(self, data: bytes | None, note: str) -> None:
        self._image_canvas.delete("all")
        self._photo = None  # 必须留引用，否则 Tk 会把图回收掉
        if data is None:
            self._image_canvas.create_text(12, 12, anchor="nw", text=note, fill="#888780")
            return
        # Tk 的 PhotoImage 只吃位图（8.6 起原生支持 PNG）；create_image 默认居中
        try:
            self._photo = tk.PhotoImage(data=base64.b64encode(data).decode("ascii"))
        except tk.TclError as exc:  # 位图坏了不该把界面搞崩
            self._image_canvas.create_text(12, 12, anchor="nw", text=f"<位图无法显示: {exc}>")
            return
        self._image_canvas.create_image(
            self._image_canvas.winfo_width() / 2,
            self._image_canvas.winfo_height() / 2,
            image=self._photo,
        )

    @staticmethod
    def _set_text(widget: tk.Text, text: str) -> None:
        if widget.get("1.0", "end-1c") == text:
            return
        at_bottom = widget.yview()[1] >= 0.999
        widget.delete("1.0", tk.END)
        widget.insert("1.0", text)
        if at_bottom:
            widget.see(tk.END)

    def _refresh_daemon_state(self) -> None:
        if self._thread is None:
            self._daemon_state.set("未启动")
            return
        pid = "有" if self._daemon.pid_path.exists() else "无"
        self._daemon_state.set(f"running={self._daemon.running} pid文件={pid}")

    # ════════════════════════════════════════════════════════════
    # 生命周期（验守护进程自己的能力）
    # ════════════════════════════════════════════════════════════

    def _start(self) -> None:
        if self._thread is not None:
            self._status.set("已经启动了")
            return
        try:
            self._daemon.start()
        except Exception as exc:  # 已有实例 / 缺依赖 / 端口被占；启动已回滚
            messagebox.showerror("启动失败", f"{type(exc).__name__}: {exc}")
            return
        self._thread = threading.Thread(target=self._daemon.run, name="owner-loop", daemon=True)
        self._thread.start()
        self._status.set(f"守护进程已启动，pid 文件 {self._daemon.pid_path}")

    def _request_stop(self) -> None:
        self._daemon.request_stop()
        self._status.set("已请求停止")

    def _stop(self) -> None:
        if self._thread is not None:
            self._daemon.request_stop()
            self._thread.join(_STOP_TIMEOUT)
            self._thread = None
        finished = self._daemon.stop(_STOP_TIMEOUT)
        self._status.set("已停止" if finished else "收尾超时（还有线程在跑）")

    def _start_second(self) -> None:
        """同一份配置再起一个：单实例锁应当把它顶回去。"""
        other = Daemon(self._config, lambda: self._handler, on_reply=lambda _r: None)
        try:
            other.start()
        except Exception as exc:
            self._status.set(f"第二个守护进程被拒（单实例）: {type(exc).__name__}: {exc}")
            return
        other.stop()
        self._status.set("警告：单实例锁没挡住第二个守护进程")

    # ════════════════════════════════════════════════════════════
    # 操作（都经接缝）
    # ════════════════════════════════════════════════════════════

    def _create_session(self) -> None:
        self._sid_seq += 1
        sid = f"t-{self._sid_seq}"
        argv = tuple(shlex.split(self._command.get().strip()))
        self._command.delete(0, tk.END)
        self._ask(Request("create", sid=sid, argv=argv, mode=self._mode.get()), "create")

    def _close_selected(self) -> None:
        if self._selected is not None:
            self._ask(Request("close", sid=self._selected), "close")

    def _send_input(self, newline: bool = False) -> None:
        """输入走字节帧：它本身就是一次操作，不需要配对的控制请求。"""
        if self._selected is None:
            self._status.set("先选一个会话")
            return
        if newline:
            # PTY 的"回车"是 CR：ConPTY 上的 cmd.exe 只认 CR 提交命令行，LF 会被留在
            # 输入缓冲里。子进程的 stdin 是普通字节流，换行保持 LF。
            mode = (self._detail or {}).get("mode")
            tail = b"\r" if mode == "pty" else b"\n"
        else:
            tail = b""
        data = self._input.get().encode() + tail
        self._input.delete(0, tk.END)
        if not self._daemon.submit_input(self._selected, data):
            self._status.set("守护进程已在收尾，这段输入被放弃了")

    def _send_interrupt(self) -> None:
        if self._selected is not None:
            self._daemon.submit_input(self._selected, b"\x03")  # Ctrl+C

    def _resize(self) -> None:
        if self._selected is None:
            return
        try:
            cols, rows = int(self._cols.get()), int(self._rows.get())
        except ValueError:
            messagebox.showwarning("改尺寸", "宽高要是整数")
            return
        self._ask(Request("resize", sid=self._selected, cols=cols, rows=rows), "resize")

    def _on_select(self, _event: object = None) -> None:
        """用户改了树的选中项 → 切换当前会话。

        `<<TreeviewSelect>>` 是**异步**派发的，且值没变也照发，程序侧切会话同样会触发它，
        所以先比一次 `_selected`，把非用户发起的那次当成 no-op。
        """
        selection = self._tree.selection()
        sid = selection[0] if selection else None
        if sid != self._selected:
            self._select_session(sid)

    def _select_session(self, sid: str | None) -> None:
        """把当前会话切到 `sid`（无会话传 `None`）：树选中、pty 专属控件、详情一起到位。"""
        self._selected = sid
        self._rendered_key = None
        selected = self._tree.selection()
        if sid is None:
            if selected:
                self._tree.selection_remove(*selected)
        elif sid not in selected:
            self._tree.selection_set(sid)
        self._detail = None
        self._sync_pty_controls(None)
        if sid is not None and self._daemon.running:
            self._ask(Request("detail", sid=sid), "detail")
        else:
            self._clear_detail()

    def _sync_pty_controls(self, detail: dict | None) -> None:
        """屏幕页 / SVG 源码页 / 导出按钮 / 尺寸控件都是 pty 专属：其他模式藏掉或禁用。"""
        is_pty = bool(detail and detail.get("is_terminal"))
        if is_pty:
            self._cols.delete(0, tk.END)
            self._cols.insert(0, str(detail.get("cols") or 80))
            self._rows.delete(0, tk.END)
            self._rows.insert(0, str(detail.get("rows") or 24))
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

    def _clear_detail(self) -> None:
        for widget in (self._view, self._raw, self._procs, self._svg):
            self._set_text(widget, "")
        self._svg_source = None
        self._rendered_key = None
        self._set_image(None, "未选中会话")

    # ════════════════════════════════════════════════════════════
    # 导出
    # ════════════════════════════════════════════════════════════

    def _save_svg(self) -> None:
        if self._svg_source is None:
            messagebox.showinfo("保存 SVG", "没有 SVG 可保存")
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
        if self._selected is None:
            return
        path = filedialog.asksaveasfilename(
            title="保存屏幕 PNG",
            defaultextension=".png",
            initialfile="screen.png",
            filetypes=[("PNG", "*.png")],
        )
        if not path:
            return
        self._export_path = path
        self._ask(
            Request("image", sid=self._selected, scale=_EXPORT_SCALE),
            "export",
        )

    def _write_export(self, blob: object) -> None:
        path, self._export_path = self._export_path, None
        if path is None or not isinstance(blob, bytes):
            self._status.set("导出失败：没拿到位图")
            return
        Path(path).write_bytes(blob)
        self._status.set(f"已保存 PNG → {path}")

    # ════════════════════════════════════════════════════════════
    # 收尾
    # ════════════════════════════════════════════════════════════

    def on_close(self) -> None:
        """窗口关闭：停守护进程（它会收尾所有会话）。"""
        if self._tick_job is not None:
            self._root.after_cancel(self._tick_job)
            self._tick_job = None
        if self._thread is not None:
            self._daemon.request_stop()
            self._thread.join(_STOP_TIMEOUT)
            self._thread = None
        self._daemon.stop(_STOP_TIMEOUT)
        self._root.destroy()


_ROWS = ("command", "mode", "state", "exit_code", "members")


def main() -> int:
    root = tk.Tk()
    app = App(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()
    return 0
