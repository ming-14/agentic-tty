"""守护进程的验证台：界面与 `core_test/gui.py` 同款，差别只在**挂载点**。

- `core_test/gui.py`：直接驱动核心层（自己装 core + runtime），不套守护进程。
- 本文件：把核心层交给守护进程承载，**一切数据都经守护进程的接缝**走一遍。

因此每个动作都是一次请求：`submit()` 投进去，答复由 `on_reply` 回调回来，界面在 tick
里取出后更新。`on_reply` **跑在所有者线程上**，所以它只入队，不碰界面。

控件复用 `example/ui/` 那一套（纯 Tk，不认识 core 也不认识守护进程），本文件只负责
**把答复翻译成界面状态**。

**它只碰守护进程的接缝**：`start` / `request_stop` / `stop` / `running` / `pid_path` /
`submit` / `submit_input` / `on_reply`。核心层由本包 `handler.py` 驱动。
"""

from __future__ import annotations

import shlex
import threading
import tkinter as tk
from pathlib import Path
from queue import Empty, Queue
from tkinter import messagebox, ttk

from ...core.runtime.host_factory import check_dependencies as check_runtime_dependencies
from ...daemon.config import DaemonConfig
from ...daemon.handler import Reply
from ...daemon.server import Daemon, SubmitOutcome
from ...foundation.logs import get_logger
from ...foundation.paths import default_runtime_dir
from ..ui import (
    EXPORT_SCALE,
    HINT_COLOR,
    DetailNotebook,
    InputBar,
    Page,
    ScreenView,
    SessionBar,
    SessionTree,
    SizeBar,
    StatusBar,
    ask_save,
)
from .handler import Answer, KernelHandler, Request

_logger = get_logger("example.daemon_test.gui")

_TICK_MS = 20
# 每多少个 tick 刷一次界面（20ms × 8 ≈ 160ms）。刷新 = 两次请求往返。
_REFRESH_EVERY = 8
_STOP_TIMEOUT = 2.0
_NAME = "agentic-tty-daemon-test"
# 答复里会话表的列（顺序即 `ui.SessionTree` 的列序）
_ROWS = ("command", "mode", "state", "exit_code", "members")


class App:
    """验证台。"""

    def __init__(self, root: tk.Tk) -> None:
        self._root = root
        self._config = DaemonConfig(name=_NAME, runtime_dir=default_runtime_dir(_NAME))
        self._handler = KernelHandler()
        self._replies: Queue[Reply] = Queue()
        self._daemon = Daemon(
            self._config,
            lambda: self._handler,
            on_reply=self._on_reply,
            check_dependencies=check_runtime_dependencies,
        )
        self._thread: threading.Thread | None = None

        self._ticks = 0
        self._sid_seq = 0
        self._selected: str | None = None
        self._sized_for: str | None = None
        """尺寸框已经为哪个会话填过一次——只在换会话时填，别把用户正在输入的宽高擦掉。"""
        self._sessions: dict[str, dict] = {}
        self._detail: dict | None = None
        # 请求 → 用途：答复回来时靠请求对象的身份认出它属于哪一次询问
        self._want: dict[int, str] = {}
        self._export_path: str | None = None
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
            daemon_row, text=f"运行时目录 {self._config.runtime_dir}", foreground=HINT_COLOR
        ).pack(side=tk.LEFT, padx=8)

        self._bar = SessionBar(
            self._root,
            modes=(("pty", "pty"), ("subprocess", "subprocess")),
            value="pty",
            buttons=(("创建会话", self._create_session), ("关闭选中", self._close_selected)),
            hint="（留空 = 平台默认 shell）",
            padding=(8, 0),
        )

        body = ttk.Panedwindow(self._root, orient=tk.HORIZONTAL)
        body.pack(fill=tk.BOTH, expand=True, padx=8, pady=(4, 6))

        left = ttk.Frame(body)
        body.add(left, weight=1)
        self._tree = SessionTree(left, on_select=self._on_select)
        self._tree.pack(fill=tk.BOTH, expand=True)

        right = ttk.Frame(body)
        body.add(right, weight=3)
        self._tabs = DetailNotebook(right)
        self._tabs.pack(fill=tk.BOTH, expand=True)
        self._screen = ScreenView(self._tabs, on_format_change=self._on_format_change)
        self._tabs.add_view_page()  # 范围由服务端给，页面上不放单选
        self._tabs.add_text(Page.RAW, "原始字节", small=True)
        self._tabs.add_text(Page.PROCS, "进程")

        self._input = InputBar(right, on_send=self._send_input, on_interrupt=self._send_interrupt)
        self._size = SizeBar(
            right,
            on_apply=self._resize,
            on_save_svg=self._save_svg,
            on_save_png=self._save_png,
        )
        self._status = StatusBar(self._root)

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
        if self._daemon.submit(request) is not SubmitOutcome.DELIVERED:
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
            # 只记选中：新会话的行还没进树（列表是异步取回来的），这时碰树会报错。
            # 下一轮 `_apply_sessions` 会把树的选中与 `_selected` 对齐。
            self._selected = str(answer.data.get("sid") or "")
            self._sized_for = None
            self._status.set(f"已创建 {self._selected}")
        elif purpose == "resize":
            self._status.set(f"尺寸已改为 {answer.data.get('cols')}×{answer.data.get('rows')}")
        elif purpose == "close":
            self._select_session(None)

    def _apply_sessions(self, rows: list[dict]) -> None:
        """增量刷新会话表：行 iid 就是 sid，选中一并对齐（细节在 `SessionTree.refresh`）。"""
        self._sessions = {str(row.get("sid")): row for row in rows}
        self._tree.refresh(
            [(sid, tuple(row.get(key) for key in _ROWS)) for sid, row in self._sessions.items()],
            selected=self._selected,
        )

    def _apply_detail(self, data: dict) -> None:
        self._detail = data
        self._tabs.set_text(Page.VIEW, str(data.get("view") or ""))
        self._tabs.set_text(Page.RAW, str(data.get("raw") or ""))
        self._tabs.set_text(Page.PROCS, str(data.get("procs") or ""))
        self._sync_pty_controls(data)
        self._refresh_screen_view(data)
        drained = "已排空" if data.get("drained") else "进行中"
        meta = " · ".join(str(x) for x in (data.get("title"), data.get("cwd")) if x)
        self._status.set(
            f"{data.get('command')} · {data.get('mode')} · {data.get('state')} · {drained}"
            f" · exit={data.get('exit_code')}" + (f" · {meta}" if meta else "")
        )

    def _refresh_screen_view(self, data: dict) -> None:
        """屏幕页 / SVG 源码页（pty 专属）：只供 SVG，渲染与缓存在 `ScreenView` 里。

        `image` 格式的位图要经请求往返取一趟，取回来由 `_show_image` 落到画布上。
        """
        sid = str(data.get("sid") or "")
        svg = data.get("svg")
        if not data.get("is_terminal") or svg is None:
            self._screen.reset(self._screen_note_for(data))
            return
        scale = self._screen.refresh(key=(sid, int(data.get("offset") or 0)), svg=str(svg))
        if scale is not None:
            self._ask(Request("image", sid=sid, scale=scale), "image")

    @staticmethod
    def _screen_note_for(data: dict) -> str:
        if not data.get("is_terminal"):
            return "该会话没有屏幕（只有子进程会话才有字节流）"
        return f"<无屏幕视图: {data.get('svg_error') or '宿主不可用'}>"

    def _on_format_change(self) -> None:
        if self._selected is not None:
            self._ask(Request("detail", sid=self._selected), "detail")

    # ════════════════════════════════════════════════════════════
    # 显示
    # ════════════════════════════════════════════════════════════

    def _show_image(self, blob: object) -> None:
        self._screen.set_image(blob if isinstance(blob, bytes) else None, "<位图不可用>")

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
        other = Daemon(
            self._config,
            lambda: self._handler,
            on_reply=lambda _r: None,
            check_dependencies=check_runtime_dependencies,
        )
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
        argv = tuple(shlex.split(self._bar.command.get().strip()))
        self._bar.command.delete(0, tk.END)
        self._ask(Request("create", sid=sid, argv=argv, mode=self._bar.mode.get()), "create")

    def _close_selected(self) -> None:
        if self._selected is not None:
            self._ask(Request("close", sid=self._selected), "close")

    def _send_input(self, newline: bool = False) -> None:
        """输入走字节帧：它本身就是一次操作，不需要配对的控制请求。"""
        if self._thread is None:
            self._status.set("守护进程没在跑——先点「启动」")
            return
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
        data = self._input.text.encode() + tail
        self._input.clear()
        if self._daemon.submit_input(self._selected, data) is not SubmitOutcome.DELIVERED:
            self._status.set("守护进程已在收尾，这段输入被放弃了")

    def _send_interrupt(self) -> None:
        if self._thread is None or self._selected is None:
            return
        self._daemon.submit_input(self._selected, b"\x03")  # Ctrl+C

    def _resize(self) -> None:
        if self._selected is None:
            return
        try:
            cols, rows = self._size.size
        except ValueError:
            messagebox.showwarning("改尺寸", "宽高要是整数")
            return
        self._ask(Request("resize", sid=self._selected, cols=cols, rows=rows), "resize")

    def _on_select(self, uid: str | None) -> None:
        """用户改了树的选中项 → 切换当前会话。

        `<<TreeviewSelect>>` 是**异步**派发的，且值没变也照发，程序侧切会话同样会触发它，
        所以先比一次 `_selected`，把非用户发起的那次当成 no-op。
        """
        if uid != self._selected:
            self._select_session(uid)

    def _select_session(self, sid: str | None) -> None:
        """把当前会话切到 `sid`（无会话传 `None`）：树选中、pty 专属控件、详情一起到位。"""
        self._selected = sid
        self._tree.set_selection(sid)
        self._detail = None
        self._sync_pty_controls(None)
        if sid is not None and self._daemon.running:
            self._ask(Request("detail", sid=sid), "detail")
        else:
            self._clear_detail()

    def _sync_pty_controls(self, detail: dict | None) -> None:
        """屏幕页 / SVG 源码页 / 导出按钮 / 尺寸控件都是 pty 专属：其他模式藏掉或禁用。

        尺寸框只在**换会话**时填一次：每次刷新都填，会把用户正在输入的宽高擦掉。
        **先解开禁用再填**：disabled 的 Entry 连程序化的 delete / insert 都会静默忽略，
        反过来做的话首次选中 pty 会话时填进去的宽高会被 Tk 吃掉。
        """
        is_pty = bool(detail and detail.get("is_terminal"))
        state = "normal" if is_pty else "hidden"
        for key in (Page.SCREEN, Page.SVG):
            self._tabs.set_page_state(key, state)
        self._size.set_enabled(is_pty)
        sid = str(detail.get("sid")) if detail else None
        if is_pty and sid is not None and sid != self._sized_for:
            self._sized_for = sid
            self._size.set_size(int(detail.get("cols") or 80), int(detail.get("rows") or 24))

    def _clear_detail(self) -> None:
        for key in (Page.VIEW, Page.RAW, Page.PROCS):
            self._tabs.set_text(key, "")
        self._screen.reset("未选中会话")

    # ════════════════════════════════════════════════════════════
    # 导出
    # ════════════════════════════════════════════════════════════

    def _save_svg(self) -> None:
        path = self._screen.save_svg()
        if path:
            self._status.set(f"已保存 SVG → {path}")

    def _save_png(self) -> None:
        if self._selected is None:
            return
        path = ask_save(
            title="保存屏幕 PNG",
            initial="screen.png",
            extension=".png",
            filetype=("PNG", "*.png"),
        )
        if not path:
            return
        self._export_path = path
        self._ask(
            Request("image", sid=self._selected, scale=EXPORT_SCALE),
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


def main() -> int:
    root = tk.Tk()
    app = App(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()
    return 0
