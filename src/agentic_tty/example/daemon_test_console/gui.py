"""守护进程的验证台：界面与 `core_test_console/gui.py` 同款，差别只在**挂载点**。

- `core_test_console/gui.py`：直接驱动核心层（自己装 core + runtime），不套守护进程。
- 本文件：**连守护进程**——界面里没有一行碰 core，一切数据都经接入点往返。

每个动作都是一次请求：`Client.request()` 投出去，答复由后台读线程投进队列，界面在 tick
里取出后更新。所以界面**只同步一个廉价队列**，从不阻塞。

控件复用 `example/ui/` 那一套（纯 Tk，不认识任何一层），本文件只负责**把答复翻译成界面
状态**。详情区**只问当前可见的那一页**——切到哪页才发哪页的请求。
"""

from __future__ import annotations

import shlex
import threading
import time
import tkinter as tk
from pathlib import Path
from queue import Empty, Queue
from tkinter import messagebox, ttk

from ...foundation.instance import is_held
from ...foundation.logs import get_logger
from ...protocol.contracts.daemon_ipc import Command, Event, Notice, ReadMode, SessionRef
from ...protocol.envelope import Envelope
from ...protocol.frame import BytesFrame
from ...protocol.response import data_of, error_of, is_ok
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
    ViewRange,
    ask_save,
)
from . import address, lock
from .client import Answer, Client

_logger = get_logger("example.daemon_test_console.gui")

_TICK_MS = 20
# 每多少个 tick 刷一次界面（20ms × 8 ≈ 160ms）；刷新 = 几次请求往返。
_REFRESH_EVERY = 8
_RAW_TAIL = 2000
"""原始字节页每轮只取尾部——整段会让每轮都在搬整份日志。"""
_SUB_KEEP_LINES = 200
"""「订阅流」页只留最后这么多行——它一直在长（TUI 满屏重绘时尤其快）。"""
_CONNECT_TRY = 0.2
"""单次连接尝试的等待上限——连不上就再来一次。"""
_CONNECT_RETRY = 0.3
"""两次尝试之间的间隔。"""
_FLOOD_BYTES = 1 << 21
"""灌输入队列用的字节数——**故意超过守护进程默认的 1 MiB 输入硬上限**，好演示 `REJECTED`。"""

_NOTICES = frozenset({Notice.INPUT_HOLD.value, Notice.INPUT_RESUME.value})
"""输入流控通知的 `type` 取值——它们不属于任何请求，按类型分派，不查 `mid` 表。"""


def _size_suffix(data: dict) -> str:
    """尺寸后缀；没有屏幕的会话没有尺寸，留空而不是写 `None×None`。"""
    cols, rows = data.get("cols"), data.get("rows")
    return f" 尺寸={cols}×{rows}" if cols is not None else ""


class App:
    """验证台：把守护进程的答复翻译成界面状态。"""

    def __init__(self, root: tk.Tk, instance: str, endpoint: str | None = None) -> None:
        self._root = root
        # 名字由入口从配置取来；地址按实例名与端点算，锁名只按实例名
        self._address = address(instance, endpoint)
        self._lock = lock(instance)
        self._answers: Queue[Answer] = Queue()
        # 请求 → (用途, 目标会话)：答复靠 mid 认领；带会话是为了丢掉"已经切走的那一个"
        self._want: dict[str, tuple[str, str | None]] = {}
        self._sessions: dict[str, SessionRef] = {}
        self._selected: str | None = None
        self._sized_for: str | None = None
        """尺寸框已经为哪个会话填过一次——只在换会话时填，别把用户正在输入的宽高擦掉。"""
        self._ticks = 0
        self._tick_job: str | None = None
        self._sub_mid: str | None = None
        """当前订阅的 id（= 那条 `subscribe` 请求的 mid）；推送与它同 mid。"""
        self._held: set[str] = set()
        """被守护进程 `INPUT_HOLD` 住的会话 uid——本端排队，别再往里发。"""

        self._client = Client(self._address, on_reply=self.on_reply)
        # 连接在**后台线程**里做：连不上要重试，而主线程得跑 mainloop 不能阻塞
        self._connected = False
        self._closing = threading.Event()
        self._states: Queue[str] = Queue()
        self._build_ui()
        self._sync_pty_controls(None)
        self._connect_thread = threading.Thread(
            target=self._connect_loop, name="daemon-connect", daemon=True
        )
        self._connect_thread.start()
        self._tick_job = self._root.after(_TICK_MS, self._tick)

    # ════════════════════════════════════════════════════════════
    # 接缝回调（跑在客户端的读线程上）
    # ════════════════════════════════════════════════════════════

    def on_reply(self, answer: Answer) -> None:
        """客户端把答复交回来。**在读线程上被调用**——只入队，不碰界面。"""
        self._answers.put(answer)

    # ════════════════════════════════════════════════════════════
    # 连接（后台线程）
    # ════════════════════════════════════════════════════════════

    def _connect_loop(self) -> None:
        """后台连守护进程，**掉了就回头重连**；每轮把"看到什么状态"投进队列，界面在 tick 里取。

        三态靠两个公共信号分——**锁在不在**（有个守护进程活着吗）＋ **连不连得上**
        （它服务得了吗）。锁在装配的**第一步**取、监听在**中后段**挂，所以：

        「锁空 + 连不上」= 没启动　「锁占 + 连不上」= 正在初始化　「连得上」= 已连接
        """
        while not self._closing.is_set():
            if not self._client.try_connect(timeout=_CONNECT_TRY):
                self._states.put("starting" if is_held(self._lock) else "down")
                time.sleep(_CONNECT_RETRY)
                continue
            self._states.put("connected")
            # 守到掉线为止——守护进程被重启时，台子得如实退回"没连上"再重连
            while not self._closing.is_set() and self._client.connected:
                time.sleep(_CONNECT_RETRY)

    def _drain_states(self) -> None:
        while True:
            try:
                state = self._states.get_nowait()
            except Empty:
                return
            self._apply_state(state)

    def _apply_state(self, state: str) -> None:
        if state == "connected":
            self._connected = True
            self._sub_mid = None  # 新连接上不存在旧订阅，留着 mid 只会把推送认错
            self._held.clear()  # 同理：新连接上不存在旧的 hold
            self._daemon_state.set("已连接，取状态中…")  # pid / uptime 等第一条答复
            return
        self._connected = False
        if state == "starting":
            self._daemon_state.set("守护进程正在初始化…（锁已占，监听还没挂上）")
        else:
            self._daemon_state.set("守护进程未启动（重试连接中…）")

    # ════════════════════════════════════════════════════════════
    # 界面
    # ════════════════════════════════════════════════════════════

    def _build_ui(self) -> None:
        self._root.title("agentic-tty · 守护进程验证台")
        self._root.geometry("1100x700")

        daemon_row = ttk.Frame(self._root, padding=(8, 6))
        daemon_row.pack(fill=tk.X)
        ttk.Label(daemon_row, text="守护进程").pack(side=tk.LEFT)
        self._daemon_state = tk.StringVar(value="正在连接守护进程…")
        ttk.Label(daemon_row, textvariable=self._daemon_state).pack(side=tk.LEFT, padx=8)
        ttk.Label(daemon_row, text=f"接入点 {self._address}", foreground=HINT_COLOR).pack(
            side=tk.LEFT, padx=8
        )

        self._bar = SessionBar(
            self._root,
            modes=(("pty", "pty"), ("localpty", "localpty"), ("subprocess", "subprocess")),
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
        # 位图在守护进程手里出：这一侧发请求，答复回来自己铺画布
        self._screen = ScreenView(
            self._tabs,
            on_format_change=self._refresh_detail,
            produce=self._request_bitmap,
        )
        self._tabs.add_view_page(self._refresh_detail)
        self._tabs.add_text(Page.RAW, "原始字节", small=True)
        self._tabs.add_text(Page.SUB, "订阅流", small=True)

        self._input = InputBar(right, on_send=self._send_input, on_interrupt=self._send_interrupt)
        self._input.add_button("订阅选中", self._subscribe_selected)
        self._input.add_button("灌输入队列", self._flood_input)
        self._size = SizeBar(
            right,
            on_apply=self._resize,
            on_save_svg=self._save_svg,
            on_save_png=self._save_png,
        )
        self._status = StatusBar(self._root)
        # 页装齐了才接切页回调——`<<NotebookTabChanged>>` 在 add 时也会发
        self._tabs.on_page_change(self._refresh_detail)

    # ════════════════════════════════════════════════════════════
    # 驱动循环
    # ════════════════════════════════════════════════════════════

    def _tick(self) -> None:
        try:
            self._drain_states()
            self._drain()
            self._ticks += 1
            # 没连上就别发请求——那些答复永远不会来
            if self._connected and self._ticks % _REFRESH_EVERY == 0:
                self._ask(Command.DAEMON_STATUS, "status")
                self._ask(Command.LIST_SESSIONS, "list")
                self._refresh_detail()
        except Exception:
            _logger.exception("驱动循环异常")
        finally:
            self._tick_job = self._root.after(_TICK_MS, self._tick)

    def _drain(self) -> None:
        while True:
            try:
                answer = self._answers.get_nowait()
            except Empty:
                return
            self._dispatch(answer)

    # ════════════════════════════════════════════════════════════
    # 请求 / 答复
    # ════════════════════════════════════════════════════════════

    def _ask(self, command: str, purpose: str, op: dict | None = None) -> None:
        """把一条请求投给守护进程；答复回来时按 `purpose` 处理。"""
        try:
            mid = self._client.request(command, op)
        except Exception as exc:  # 连接断了
            self._status.set(f"发送失败: {exc}")
            return
        self._want[mid] = (purpose, op.get("uid") if op else None)

    def _dispatch(self, answer: Answer) -> None:
        envelope = answer.envelope
        if envelope is not None and envelope.type in _NOTICES:
            self._apply_notice(envelope)
            return
        if answer.mid == self._sub_mid:
            self._apply_push(answer)
            return
        entry = self._want.pop(answer.mid, None)
        if entry is None:
            return
        purpose, uid = entry
        if uid is not None and uid != self._selected:
            return  # 答复属于已经切走的会话：画上去就是错配
        if answer.chunk is not None:
            self._apply_chunk(purpose, answer.chunk)
            return
        envelope = answer.envelope
        if envelope is None:
            return
        if not is_ok(envelope):
            failure = error_of(envelope)
            self._status.set(f"失败（{purpose}）: {failure.message if failure else '未知错误'}")
            return
        self._apply_data(purpose, data_of(envelope))

    def _apply_data(self, purpose: str, data: dict) -> None:
        if purpose == "status":
            self._daemon_state.set(
                f"pid={data.get('pid')} · 运行 {data.get('uptime')}s"
                f" · 会话 {data.get('sessions')}"
            )
        elif purpose == "list":
            self._apply_sessions(data.get("sessions") or [])
        elif purpose == "create":
            session = SessionRef.from_dict(data["session"])
            if session.uid != self._selected:
                self._unsubscribe()  # 订阅绑着会话，换了就退掉
            self._selected = session.uid
            self._sized_for = None
            self._status.set(f"已创建 {session.command}（{session.mode}）uid={session.uid[:8]}")
        elif purpose == "view":
            # 答复回来时那一页可能已经被切走——那就别替它重排文本
            if self._tabs.current is Page.VIEW:
                self._tabs.set_text(Page.VIEW, str(data.get("text") or ""))
        elif purpose in ("svg_screen", "svg_source"):
            self._apply_svg(purpose, data)
        elif purpose == "resize":
            self._status.set(f"尺寸已改为 {data.get('cols')}×{data.get('rows')}")
        elif purpose == "close":
            self._select_session(None)

    def _apply_chunk(self, purpose: str, chunk: BytesFrame) -> None:
        if purpose == "raw" and self._tabs.current is Page.RAW:
            self._tabs.set_text(Page.RAW, repr(chunk.data))
        elif purpose == "image" and self._tabs.current is Page.SCREEN:
            self._screen.set_image(chunk.data, "")

    def _request_bitmap(self, scale: float) -> None:
        """`image` 格式的位图在守护进程手里出——发请求，到了再由 `_apply_chunk` 铺上。

        返回 `None` 表示"还在路上"：画布先留着上一张，别清成空白。
        """
        uid = self._selected
        if uid is not None:
            self._ask(
                Command.READ_SESSION,
                "image",
                {"uid": uid, "mode": ReadMode.IMAGE.value, "scale": scale},
            )
        return None

    # ════════════════════════════════════════════════════════════
    # 订阅
    # ════════════════════════════════════════════════════════════

    def _subscribe_selected(self) -> None:
        """订阅选中会话的 stdout——之后不再靠每轮拉取，守护进程按游标把增量推过来。"""
        uid = self._selected
        if uid is None:
            self._status.set("先选一个会话")
            return
        self._unsubscribe()
        try:
            mid = self._client.request(
                Command.SUBSCRIBE, {"uid": uid, "stream": "stdout"}
            )
        except Exception as exc:
            self._status.set(f"订阅失败: {exc}")
            return
        self._sub_mid = mid
        self._tabs.set_text(Page.SUB, f"── 订阅 {uid[:8]} 的 stdout ──\n")

    def _unsubscribe(self) -> None:
        mid, self._sub_mid = self._sub_mid, None
        if mid is not None:
            self._ask(Command.UNSUBSCRIBE, "unsubscribe", {"sub_id": mid})

    def _apply_push(self, answer: Answer) -> None:
        """订阅推送：字节进「订阅流」页，控制帧按类型记一行。"""
        if answer.chunk is not None:
            self._append_sub(repr(answer.chunk.data))
            return
        envelope = answer.envelope
        if envelope is None:
            return
        if not is_ok(envelope):  # 订阅本身失败（uid 不存在 / 流不对）
            failure = error_of(envelope)
            self._append_sub(
                f"── 订阅失败: {failure.message if failure else '未知错误'} ──\n", always=True
            )
            self._sub_mid = None
            return
        data = data_of(envelope)
        if envelope.type == Event.ENDED:
            self._append_sub(f"── 结束 exit={data.get('exit_code')} ──\n", always=True)
            self._sub_mid = None
        elif envelope.type == Event.RESYNC:
            self._append_sub(
                f"── 重同步 lossy={data.get('lossy')}{_size_suffix(data)} ──\n", always=True
            )
        elif envelope.type == Event.RESIZE:
            self._append_sub(
                f"── 尺寸 {data.get('cols')}×{data.get('rows')} @ {data.get('offset')} ──\n",
                always=True,
            )
        else:  # ack：`type` 就是 subscribe 那条命令
            self._append_sub(
                f"── 已订阅 offset={data.get('offset')} lossy={data.get('lossy')}"
                f"{_size_suffix(data)} ──\n",
                always=True,
            )

    def _append_sub(self, text: str, *, always: bool = False) -> None:
        """往「订阅流」页追加。

        字节推送很密（满屏重绘时尤甚），**只在那一页看得见时才写**；控制帧（订阅失败 /
        结束 / 重同步）是订阅本身的状态，人在哪一页都得留下。
        """
        if not always and self._tabs.current is not Page.SUB:
            return
        box = self._tabs.text(Page.SUB)
        box.insert(tk.END, text)
        box.delete("1.0", f"end-{_SUB_KEEP_LINES}l")  # 内容不足时 Tk 夹到 1.0，等于不删
        box.see(tk.END)

    def _apply_notice(self, envelope: Envelope) -> None:
        """输入流控：`INPUT_HOLD` 就本端排队（不再往里发），`INPUT_RESUME` 就放行。

        `mid` 是会话 uid——通知不挂在任何请求上，所以不查 `_want` 表。
        """
        uid = envelope.mid
        if envelope.type == Notice.INPUT_HOLD.value:
            self._held.add(uid)
            self._status.set(f"输入越软水位：{uid[:8]} 本端排队（等排空）")
        else:
            self._held.discard(uid)
            self._status.set(f"输入已排空：{uid[:8]} 可继续发")

    def _apply_svg(self, purpose: str, data: dict) -> None:
        """矢量来自守护进程，位图在客户端出；栅格化那步由 `ScreenView` 按内容去重。"""
        svg = data.get("text")
        if not isinstance(svg, str):
            return
        page = Page.SVG if purpose == "svg_source" else Page.SCREEN
        if self._tabs.current is not page:
            return  # 那一页已经切走了；栅格化 170 ms，替看不见的页付不值
        if purpose == "svg_source":
            self._screen.show_source(svg)
        else:
            self._screen.show_screen(svg)

    def _apply_sessions(self, rows: list[dict]) -> None:
        """增量刷新会话表：行 iid 就是 uid，选中一并对齐（细节在 `SessionTree.refresh`）。"""
        self._sessions = {str(row.get("uid")): SessionRef.from_dict(row) for row in rows}
        self._tree.refresh(
            [
                (ref.uid, (ref.command, ref.mode, ref.state, ref.exit_code, ref.members))
                for ref in self._sessions.values()
            ],
            selected=self._selected,
        )
        # 会话刚出现在表里，pty 专属控件这时才认得出来——新建的会话不走 `_select_session`，
        # 少了这一句屏幕页 / 尺寸框要等用户点一下才出来。
        self._sync_pty_controls(self._selected)

    def _refresh_detail(self) -> None:
        """**只问当前可见的那一页**要数据。

        每取一次都是一次往返（请求 + 答复），替看不见的页问就是白跑一趟网络加一趟
        渲染；屏幕页那条尤其贵——答复回来还要栅格化（满屏 170 ms 上下）。
        """
        uid = self._selected
        if uid is None:
            self._clear_detail()
            return
        page = self._tabs.current
        terminal = self._is_terminal(uid)
        if page is Page.VIEW:
            if terminal:
                full = self._tabs.view_mode.get() == ViewRange.FULL
                mode = ReadMode.TEXT.value if full else ReadMode.SCREEN.value
                self._ask(Command.READ_SESSION, "view", {"uid": uid, "mode": mode})
            else:
                self._tabs.set_text(Page.VIEW, "（只有终端会话有屏幕；字节流见「原始字节」页）")
        elif page is Page.SCREEN or page is Page.SVG:
            if terminal:
                # 两页要的是同一份矢量，只是拿回来画的地方不同
                purpose = "svg_screen" if page is Page.SCREEN else "svg_source"
                self._ask(Command.READ_SESSION, purpose, {"uid": uid, "mode": ReadMode.SVG.value})
            else:
                self._screen.reset("该会话没有屏幕（只有子进程会话才有字节流）")
        elif page is Page.RAW:
            self._ask(
                Command.READ_SESSION,
                "raw",
                {"uid": uid, "mode": ReadMode.BYTES.value, "tail": _RAW_TAIL},
            )

    def _clear_detail(self) -> None:
        """没有选中会话：清空详情区（内容没变的页 `set_text` 自己不会再动）。"""
        self._tabs.set_text(Page.VIEW, "")
        self._tabs.set_text(Page.RAW, "")
        self._screen.reset("未选中会话")

    # ════════════════════════════════════════════════════════════
    # 操作（都经接缝）
    # ════════════════════════════════════════════════════════════

    def _create_session(self) -> None:
        text = self._bar.command.get().strip()
        argv = list(shlex.split(text)) if text else []
        self._bar.command.delete(0, tk.END)
        self._ask(
            Command.CREATE_SESSION,
            "create",
            {"mode": self._bar.mode.get(), "argv": argv},
        )

    def _close_selected(self) -> None:
        if self._selected is not None:
            self._ask(Command.CLOSE_SESSION, "close", {"uid": self._selected})

    def _send_input(self, newline: bool = False) -> None:
        """输入走字节帧：它本身就是一次操作，不需要配对的控制请求。

        被守护进程 hold 住的会话**本端排队**：不再往里发，等 `INPUT_RESUME` 回来。
        """
        uid = self._selected
        if uid is None:
            self._status.set("先选一个会话")
            return
        if uid in self._held:
            self._status.set("该会话输入越了软水位，本端排队中（等排空再发）")
            return
        tail = b""
        if newline:
            # PTY 的"回车"是 CR：ConPTY 上的 cmd.exe 只认 CR 提交命令行，LF 会被留在
            # 输入缓冲里。子进程的 stdin 是普通字节流，换行保持 LF。
            tail = b"\r" if self._is_terminal(uid) else b"\n"
        data = self._input.text.encode() + tail
        self._input.clear()
        try:
            self._client.write(uid, data)
        except Exception as exc:
            self._status.set(f"发送失败: {exc}")
            return
        self._status.set(f"已发出 {len(data)} 字节（由守护进程的写线程写出）")

    def _flood_input(self) -> None:
        """一次灌过硬上限：演示守护进程**整块拒收并断连**，而不是静默丢字节。"""
        uid = self._selected
        if uid is None:
            self._status.set("先选一个会话")
            return
        if uid in self._held:
            self._status.set("该会话输入越了软水位，本端排队中（等排空再灌）")
            return
        blob = b"x" * _FLOOD_BYTES
        try:
            self._client.write(uid, blob)
        except Exception as exc:
            self._status.set(f"发送失败: {exc}")
            return
        self._status.set(f"已灌入 {len(blob)} 字节——超硬上限时守护进程会断掉这条连接")

    def _send_interrupt(self) -> None:
        uid = self._selected
        if uid is None:
            return
        try:
            self._client.write(uid, b"\x03")  # Ctrl+C
        except Exception as exc:
            self._status.set(f"发送失败: {exc}")
            return
        self._status.set("已发出 Ctrl+C")

    def _resize(self) -> None:
        uid = self._selected
        if uid is None:
            return
        try:
            cols, rows = self._size.size
        except ValueError:
            messagebox.showwarning("改尺寸", "宽高要是整数")
            return
        self._ask(Command.RESIZE_SESSION, "resize", {"uid": uid, "cols": cols, "rows": rows})

    def _on_select(self, uid: str | None) -> None:
        """用户改了树的选中项 → 切换当前会话。

        `<<TreeviewSelect>>` 是**异步**派发的，且值没变也照发，程序侧切会话同样会触发它，
        所以先比一次 `_selected`，把非用户发起的那次当成 no-op。
        """
        if uid != self._selected:
            self._select_session(uid)

    def _select_session(self, uid: str | None) -> None:
        """把当前会话切到 `uid`（无会话传 `None`）：树选中、pty 专属控件、详情一起到位。"""
        if uid != self._selected:
            self._unsubscribe()  # 订阅绑着会话，换会话就退掉
        self._selected = uid
        self._tree.set_selection(uid)
        self._sized_for = None
        self._sync_pty_controls(uid)
        self._refresh_detail()

    def _sync_pty_controls(self, uid: str | None) -> None:
        """屏幕页 / SVG 源码页 / 导出按钮 / 尺寸控件都是 pty 专属：其他模式藏掉或禁用。

        尺寸框只在**换会话**时填一次：每次刷新都填，会把用户正在输入的宽高擦掉。
        """
        ref = self._sessions.get(uid) if uid is not None else None
        is_pty = ref is not None and ref.cols is not None  # 只有终端会话带尺寸
        state = "normal" if is_pty else "hidden"
        for key in (Page.SCREEN, Page.SVG):
            self._tabs.set_page_state(key, state)
        self._size.set_enabled(is_pty)
        if is_pty and uid is not None and uid != self._sized_for and ref is not None:
            self._sized_for = uid
            self._size.set_size(int(ref.cols or 80), int(ref.rows or 24))

    def _is_terminal(self, uid: str) -> bool:
        ref = self._sessions.get(uid)
        return ref is not None and ref.cols is not None

    # ════════════════════════════════════════════════════════════
    # 导出
    # ════════════════════════════════════════════════════════════

    def _save_svg(self) -> None:
        path = self._screen.save_svg()
        if path:
            self._status.set(f"已保存 SVG → {path}")

    def _save_png(self) -> None:
        """把当前屏幕的矢量栅格化后存盘——位图在客户端出，守护进程只给矢量。"""
        svg = self._screen.svg_source
        if svg is None:
            messagebox.showinfo("保存 PNG", self._screen.note or "没有屏幕可保存")
            return
        path = ask_save(
            title="保存屏幕 PNG",
            initial="screen.png",
            extension=".png",
            filetype=("PNG", "*.png"),
        )
        if not path:
            return
        data, note = ScreenView.rasterize(svg, EXPORT_SCALE)
        if data is None:
            messagebox.showinfo("保存 PNG", note)
            return
        Path(path).write_bytes(data)
        self._status.set(f"已保存 PNG → {path}")

    # ════════════════════════════════════════════════════════════
    # 收尾
    # ════════════════════════════════════════════════════════════

    def on_close(self) -> None:
        """窗口关闭：断开与守护进程的连接（会话是守护进程的，不随窗口消失）。"""
        if self._tick_job is not None:
            self._root.after_cancel(self._tick_job)
            self._tick_job = None
        self._closing.set()  # 让连接线程收工
        self._connect_thread.join(_CONNECT_RETRY * 2)
        self._client.close()
        self._root.destroy()


def main(instance: str, endpoint: str | None = None) -> int:
    root = tk.Tk()
    try:
        app = App(root, instance, endpoint)  # 连不上会抛 TransportError，交给入口去报
    except Exception:
        root.destroy()  # 别留一个空窗口
        raise
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()
    return 0
