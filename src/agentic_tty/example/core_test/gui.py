"""示例层的 Tk 管理台：接入核心层接口，手动起会话、看屏幕、发输入。

    python -m agentic_tty.example.core_test

**Tk 的 mainloop 就是所有者线程**：界面回调与 `Runtime.pump_all()` 都跑在同一
线程，所以这里不需要任何锁——这正是核心层"单线程所有者"约定带来的好处。
宿主的读由 core.runtime 的读线程代劳（`SessionRunner`），界面线程从不阻塞。

**事件驱动**：读线程拿到数据后经 `Wakeup` 唤醒；后台线程阻塞等它，把会话 uid 投进
Tk 队列，主线程只在 `_tick` 里 drain 这个廉价队列。因为 **Tk 的 `mainloop` 占着主
线程、不能阻塞**，"阻塞等"只能交给后台线程。`_tick` 仍留一个兜底周期——进程退出、
进程树变化这类没有读线程事件，只能靠它扫到。

模式四选一：`fake` 跑示例假程序（命令框下拉即假程序名）；`pty` / `localpty` /
`subprocess` 跑真命令（命令框直接输入，留空 = 平台默认 shell）。输入队列的**小水位**
由 `sessions.create_runtime` 注入，好让 `HOLD` / `REJECTED` 在台子上碰得到。

本文件只做**编排**（会话、运行时驱动、唤醒线程、订阅、刷新节奏）：

- 界面控件全在 `example/ui/`——纯 Tk，不认识 core；
- 会话 → 文本的渲染在 `render.py`——认识 core，不碰 Tk。
"""

from __future__ import annotations

import shlex
import threading
import tkinter as tk
from pathlib import Path
from queue import Empty, Queue
from tkinter import messagebox, simpledialog, ttk

from ...core.errors import CoreError
from ...core.process.session import ProcessSession
from ...core.runtime.bridge import Wakeup
from ...core.runtime.input_queue import InputVerdict
from ...core.runtime.runner import PumpEvent
from ...core.runtime.shell import default_shell
from ...core.session.base import Session
from ...core.session.subscription import Subscription
from ...core.terminal.session import TerminalSession
from ...foundation.logs import get_logger
from ..ui import (
    EXPORT_SCALE,
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
from . import render
from .programs import PROGRAMS
from .sessions import EXAMPLE_INPUT_MAX_BYTES, ExampleMode, create_runtime, session_spec

_logger = get_logger("example.core_test.gui")

_TICK_MS = 20
# 每多少个 tick 刷一次界面（20ms × 8 ≈ 160ms，避免文本频繁重排）
_REFRESH_EVERY = 8
# 收尾时等释放线程的上限；到点就放手，别挡关窗
_RELEASE_JOIN_SECONDS = 5.0
# 唤醒线程的等待粒度：它靠这个周期检查"该收工了吗"
_WAKE_POLL = 0.2
# 「事件」页只留最后这么多行——它每轮都在长
_EVENT_KEEP_LINES = 200


class App:
    """管理台。

    会话与它的驱动（读 / 写线程）**成对**交给 `core.runtime.Runtime` 管，界面不再自己
    维护「uid → 驱动」；选中哪个会话就按 uid 向它取。
    """

    def __init__(self, root: tk.Tk) -> None:
        self._root = root
        self._selected: str | None = None
        self._ticks = 0
        self._tick_job: str | None = None  # 挂起的定时任务；收尾必须先取消，否则销毁后仍会触发
        # 事件通道：读线程有活 → Wakeup → 唤醒线程 → Tk 队列 → 主线程（Tk 不能阻塞）
        self._wakeup = Wakeup()
        self._woken: Queue[str] = Queue()
        self._closing = threading.Event()
        self._wake_thread = threading.Thread(
            target=self._wake_loop, name="wakeup", daemon=True
        )
        # 会话与它的驱动**成对**交给 core.runtime：注册表、读写线程、两阶段释放都在它那
        self._runtime = create_runtime(wakeup=self._wakeup)
        # 订阅流页：用 core 的 Subscription 按游标取增量（演示订阅机制）
        self._subscription: Subscription | None = None
        self._sub_uid: str | None = None
        self._sub_pull_max: int | None = None  # 每次拉取上限；None = 一次把日志尾部全给
        self._events_uid: str | None = None  # 「事件」页当前记的是哪个会话
        self._build_ui()
        self._select_session(None)  # 初始无会话：pty 专属控件按此状态摆好
        self._wake_thread.start()
        self._tick_job = self._root.after(_TICK_MS, self._tick)

    # ════════════════════════════════════════════════════════════
    # 界面
    # ════════════════════════════════════════════════════════════

    def _build_ui(self) -> None:
        self._root.title("agentic-tty · core 管理台")
        self._root.geometry("1040x660")

        self._bar = SessionBar(
            self._root,
            modes=(
                ("fake", ExampleMode.FAKE.value),
                ("pty", ExampleMode.PTY.value),
                ("localpty", ExampleMode.LOCALPTY.value),
                ("subprocess", ExampleMode.SUBPROCESS.value),
            ),
            value=ExampleMode.FAKE.value,
            command_values=sorted(PROGRAMS),
            buttons=(
                ("创建会话", self._create_session),
                ("强杀", self._kill_selected),
                ("关闭选中", self._close_selected),
            ),
            hint="（fake 下拉选假程序；pty / localpty / subprocess 直接输入真命令）",
            on_mode_change=self._sync_command_box,
        )
        # 初始模式是 fake，命令框按它摆（默认 repl）——不能只等用户点单选才填
        self._sync_command_box()

        body = ttk.Panedwindow(self._root, orient=tk.HORIZONTAL)
        body.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 6))

        left = ttk.Frame(body)
        body.add(left, weight=1)
        self._tree = SessionTree(left, on_select=self._on_select)
        self._tree.pack(fill=tk.BOTH, expand=True)

        right = ttk.Frame(body)
        body.add(right, weight=3)
        self._tabs = DetailNotebook(right)
        self._tabs.pack(fill=tk.BOTH, expand=True)
        self._screen = ScreenView(self._tabs)
        self._tabs.add_view_page(self._refresh_detail)
        self._tabs.add_text(Page.CELLS, "格栅")
        self._tabs.add_text(Page.RAW, "原始字节", small=True)
        self._tabs.add_text(Page.SUB, "订阅流", small=True)
        self._tabs.add_text(Page.EVENTS, "事件", small=True)
        self._tabs.add_text(Page.PROCS, "进程")

        self._input = InputBar(right, on_send=self._send_input, on_interrupt=self._send_interrupt)
        self._stdin_btn = self._input.add_button("关 stdin", self._close_stdin)
        self._input.add_button("灌满输入队列", self._flood_input)
        self._input.add_button("订阅批量", self._set_pull_batch)
        self._size = SizeBar(
            right,
            on_apply=self._resize,
            on_save_svg=self._save_svg,
            on_save_png=self._save_png,
        )
        self._status = StatusBar(self._root)

    # ════════════════════════════════════════════════════════════
    # 驱动循环（所有者线程 = Tk 主线程）
    # ════════════════════════════════════════════════════════════

    def _wake_loop(self) -> None:
        """后台线程：阻塞等唤醒通道，有活就把会话 uid 投进 Tk 队列。

        单开一个线程是因为 **Tk 主线程不能阻塞**——它得跑 `mainloop`。所以这里阻塞
        等 `Wakeup`，主线程只在 `_tick` 里 drain 一个廉价队列。
        """
        while not self._closing.is_set():
            uid = self._wakeup.wait(timeout=_WAKE_POLL)
            if uid is not None:
                self._woken.put(uid)

    def _drain_woken(self) -> set[str]:
        """取走本轮的唤醒信号（Tk 主线程）。"""
        woken: set[str] = set()
        while True:
            try:
                woken.add(self._woken.get_nowait())
            except Empty:
                break
        return woken

    def _tick(self) -> None:
        try:
            woken = self._drain_woken()
            self._ticks += 1
            # 有活就立刻处理；另外每 _REFRESH_EVERY 轮兜底一次（进程退出、进程树
            # 变化这类没有读线程事件，只能靠兜底扫到）
            if woken or self._ticks % _REFRESH_EVERY == 0:
                self._refresh_events(self._runtime.pump_all())
                self._refresh_tree()
                self._refresh_detail()
        except Exception:
            _logger.exception("驱动循环异常")
        finally:
            self._tick_job = self._root.after(_TICK_MS, self._tick)

    # ════════════════════════════════════════════════════════════
    # 操作
    # ════════════════════════════════════════════════════════════

    def _selected_session(self) -> Session | None:
        """当前选中的会话；没选中或已被摘除则为 None。"""
        return self._runtime.find(self._selected) if self._selected else None

    def _sync_command_box(self) -> None:
        """按当前模式摆命令框：fake 给假程序下拉并默认 repl，真形态清空留给自由输入。"""
        if self._bar.mode.get() == ExampleMode.FAKE.value:
            self._bar.command["values"] = sorted(PROGRAMS)
            if self._bar.command.get() not in PROGRAMS:
                self._bar.command.set("repl")
        else:
            self._bar.command["values"] = []
            self._bar.command.set("")

    def _create_session(self) -> None:
        text = self._bar.command.get().strip()
        mode = ExampleMode(self._bar.mode.get())
        if text:
            # 命令按 shell 语义拆分，支持 "cmd.exe /c dir" 这种整串
            argv = tuple(shlex.split(text)) or (text,)
        elif mode is ExampleMode.FAKE:
            messagebox.showwarning("创建会话", "fake 模式要选一个假程序")
            return
        else:
            argv = default_shell()  # 留空 = 平台默认 shell
        try:
            session = self._runtime.create(session_spec(mode, argv))
        except Exception as exc:  # 宿主起不来：Runtime 不会留残骸
            messagebox.showerror("创建会话失败", str(exc))
            return
        self._status.set(f"已创建 {' '.join(argv)}（{mode}）uid={session.uid[:8]}")
        self._refresh_tree()  # 新行先进表，选中它才有意义
        self._select_session(session.uid)

    def _close_selected(self) -> None:
        """关闭选中会话：**同步摘除 + 线程里释放**（两阶段都在 `Runtime` 里）。

        宿主关闭在部分平台上会长时间阻塞（Windows 上要等控制台客户端退出），压在
        所有者线程（Tk 主线程）上会把界面冻住。所以先摘除——会话立刻从列表消失、
        不再被轮询——再把耗时的释放交给别的线程。
        """
        session = self._selected_session()
        if session is None:
            return
        self._runtime.close(session.uid)
        self._status.set(f"已关闭 uid={session.uid[:8]}")
        self._refresh_tree()
        self._select_session(None)

    def _kill_selected(self) -> None:
        """强杀进程树但**保留会话**——与「关闭选中」的区别：后者还会释放宿主。

        保留的会话仍可查输出、看退出码，是 core 生命周期里独立的一档。
        """
        session = self._selected_session()
        if session is None:
            return
        try:
            session.stop()
        except Exception as exc:
            messagebox.showerror("强杀失败", str(exc))
            return
        self._status.set(f"已强杀 uid={session.uid[:8]}（会话保留，仍可查输出）")
        self._refresh_tree()
        self._refresh_detail()

    def _close_stdin(self) -> None:
        """向子进程关 stdin 发 EOF（`cat`、`python -` 这类在等它）。"""
        session = self._selected_session()
        if not isinstance(session, ProcessSession):
            return
        try:
            session.close_stdin()
        except Exception as exc:
            messagebox.showerror("关 stdin 失败", str(exc))
            return
        self._status.set("已关闭 stdin（子进程收到 EOF）")

    def _send_input(self, newline: bool = False) -> None:
        session = self._selected_session()
        if session is None:
            return
        tail = b""
        if newline:
            # PTY 的"回车"是 CR：ConPTY 上的 cmd.exe 只认 CR 提交命令行，LF 会被留在
            # 输入缓冲里。子进程的 stdin 是普通字节流，换行保持 LF。
            tail = b"\r" if isinstance(session, TerminalSession) else b"\n"
        data = self._input.text.encode() + tail
        verdict = self._submit(session, data)
        if verdict is None:
            return
        if verdict is InputVerdict.REJECTED:
            messagebox.showwarning("发送失败", "输入超过硬上限，本次输入被拒绝")
            return
        self._input.clear()
        hint = "（越软水位：应让发送方本端排队）" if verdict is InputVerdict.HOLD else ""
        self._status.set(f"已入队 {len(data)} 字节（由写线程写出）{hint}")

    def _send_interrupt(self) -> None:
        session = self._selected_session()
        if session is not None and self._submit(session, b"\x03") is not None:  # Ctrl+C
            self._status.set("已入队 Ctrl+C")

    def _flood_input(self) -> None:
        """一次灌到硬上限之上：演示 `REJECTED`——整块拒收，绝不静默丢半个。"""
        session = self._selected_session()
        if session is None:
            return
        blob = b"x" * (EXAMPLE_INPUT_MAX_BYTES + 1)
        verdict = self._submit(session, blob)
        if verdict is not None:
            self._status.set(
                f"灌入 {len(blob)} 字节 → {verdict}（硬上限 {EXAMPLE_INPUT_MAX_BYTES}B）"
            )

    def _set_pull_batch(self) -> None:
        """设订阅流页每次拉取的上限；0 = 不设上限（一次把日志尾部整段复制出来）。"""
        value = simpledialog.askinteger(
            "订阅批量",
            "每次至多拉多少字节（0 = 不限）",
            initialvalue=self._sub_pull_max or 0,
            minvalue=0,
        )
        if value is None:
            return
        self._sub_pull_max = value or None
        self._status.set(f"订阅批量 = {self._sub_pull_max or '不限'}")

    def _submit(self, session: Session, data: bytes) -> InputVerdict | None:
        """把一段字节交给该会话的写线程；失败时提示并返回 None。"""
        try:
            return self._runtime.send_input(session.uid, data)
        except CoreError as exc:  # 会话刚被摘掉、驱动已释放
            messagebox.showerror("发送失败", str(exc))
            return None

    def _resize(self) -> None:
        session = self._selected_session()
        if session is None:
            return
        try:
            cols, rows = self._size.size
            session.resize(cols, rows)
        except Exception as exc:
            messagebox.showerror("改尺寸失败", str(exc))
            return
        self._status.set(f"尺寸已改为 {cols}×{rows}")

    def _on_select(self, uid: str | None) -> None:
        """用户改了树的选中项 → 切换当前会话。

        `<<TreeviewSelect>>` 是**异步**派发的，且值没变也照发，程序侧切会话同样会触发它，
        所以先比一次 `_selected`，把非用户发起的那次当成 no-op。
        """
        if uid != self._selected:
            self._select_session(uid)

    def _select_session(self, uid: str | None) -> None:
        """把当前会话切到 `uid`（无会话传 `None`）：树选中、pty 专属控件、详情一起到位。

        程序侧改选中必须走这里：只改 `_selected` 而不动树，界面就会与实际不一致。
        """
        self._selected = uid
        self._tree.set_selection(uid)
        self._sync_pty_controls()
        self._refresh_detail()

    def _sync_pty_controls(self) -> None:
        """按会话形态开关专属控件：pty 有屏幕 / 格栅 / 导出 / 尺寸，子进程有关 stdin。"""
        session = self._selected_session()
        terminal = session if isinstance(session, TerminalSession) else None
        state = "normal" if terminal is not None else "hidden"
        for key in (Page.SCREEN, Page.SVG, Page.CELLS):
            self._tabs.set_page_state(key, state)
        self._size.set_enabled(terminal is not None)
        is_process = isinstance(session, ProcessSession)
        self._stdin_btn.state(["!disabled"] if is_process else ["disabled"])
        if terminal is not None:
            # 尺寸框跟着会话走：留着上一个会话的宽高，点「应用」会改到别的会话头上
            self._size.set_size(terminal.cols, terminal.rows)
            self._tabs.show_page(Page.SCREEN)

    # ════════════════════════════════════════════════════════════
    # 刷新
    # ════════════════════════════════════════════════════════════

    def _refresh_tree(self) -> None:
        self._tree.refresh([(s.uid, render.row_values(s)) for s in self._runtime.list()])

    def _refresh_detail(self) -> None:
        session = self._selected_session()
        if session is None:
            for key in (Page.VIEW, Page.CELLS, Page.RAW, Page.PROCS, Page.SUB, Page.EVENTS):
                self._tabs.set_text(key, "")
            self._subscription = None
            self._sub_uid = None
            self._screen.reset("未选中会话")
            return
        full = self._tabs.view_mode.get() == ViewRange.FULL
        self._tabs.set_text(Page.VIEW, render.view_text(session, full=full))
        self._tabs.set_text(Page.CELLS, render.cells_text(session))
        self._tabs.set_text(Page.RAW, render.raw_text(session))
        self._tabs.set_text(Page.PROCS, render.processes_text(session))
        self._refresh_subscription(session)
        self._refresh_screen_views(session)
        self._status.set(render.status_text(session) + self._input_state(session))

    def _input_state(self, session: Session) -> str:
        """状态栏上的输入队列那一段：积压字节数与是否已回落。"""
        runner = self._runtime.runner(session.uid)
        if runner is None:
            return ""
        return render.input_text(runner.input_depth, runner.input_drained)

    def _refresh_events(self, events: dict[str, list[PumpEvent]]) -> None:
        """「事件」页：`pump_all()` 交出的本轮事件——消费者靠它知道"哪一路进了哪段"。

        换会话就清空（那些事件属于上一个会话）。
        """
        session = self._selected_session()
        uid = session.uid if session is not None else None
        if self._events_uid != uid:
            self._events_uid = uid
            self._tabs.set_text(Page.EVENTS, "")
        if session is None:
            return
        for line in render.event_lines(events.get(uid, ())):
            self._append(Page.EVENTS, line, keep_lines=_EVENT_KEEP_LINES)

    def _refresh_subscription(self, session: Session) -> None:
        """订阅流页：用 core 的 `Subscription` 按游标取增量——这就是订阅机制的用法。

        换会话就重建订阅；**游标从当前末尾起**，所以页里只有"订阅之后的新增"。
        尺寸变更与字节共用同一个 offset 空间、随拉取一并交出，这里先列标记（带 offset）
        再铺字节。设了「订阅批量」就是分片拉取，每片把它的 offset 窗口也标出来。
        """
        if self._subscription is None or self._sub_uid != session.uid:
            self._subscription = Subscription(session, cursor=session.journal.end_offset)
            self._sub_uid = session.uid
            self._tabs.set_text(Page.SUB, "")
            self._append(Page.SUB, f"── 订阅自 offset {self._subscription.next_offset} ──\n")
        pull = self._subscription.pull(max_bytes=self._sub_pull_max)
        if self._sub_pull_max is not None and pull.data:
            self._append(Page.SUB, f"── 分片 [{pull.start}, {self._subscription.next_offset}) ──\n")
        for event in pull.resizes:
            self._append(Page.SUB, f"── 尺寸 {event.cols}×{event.rows} @offset {event.offset} ──\n")
        if pull.data:
            self._append(Page.SUB, pull.data.decode("utf-8", errors="replace"))

    def _append(self, key: Page, text: str, *, keep_lines: int | None = None) -> None:
        """往文本页追加；`keep_lines` 限制保留的行数（「事件」页每轮都在长）。"""
        page = self._tabs.text(key)
        page.insert(tk.END, text)
        if keep_lines is not None:
            page.delete("1.0", f"end-{keep_lines}l")  # 内容不足时 Tk 夹到 1.0，等于不删
        page.see(tk.END)

    def _refresh_screen_views(self, session: Session) -> None:
        """屏幕页 / SVG 源码页（pty 专属）：取数据，渲染与缓存都在 `ScreenView` 里。"""
        if not isinstance(session, TerminalSession):
            return
        try:
            svg = session.render_svg()
        except Exception as exc:  # 宿主已关闭等
            self._screen.reset(f"<无屏幕视图: {exc}>")
            return
        self._screen.refresh(key=(session.uid, session.journal.end_offset), svg=svg)

    def _save_svg(self) -> None:
        path = self._screen.save_svg()
        if path:
            self._status.set(f"已保存 SVG → {path}")

    def _save_png(self) -> None:
        session = self._selected_session()
        if session is None:
            return
        try:
            data = render.screen_png(session, EXPORT_SCALE)
        except Exception as exc:
            messagebox.showinfo("保存 PNG", f"无屏幕位图: {exc}")
            return
        path = ask_save(
            title="保存屏幕 PNG",
            initial="screen.png",
            extension=".png",
            filetype=("PNG", "*.png"),
        )
        if not path:
            return
        Path(path).write_bytes(data)
        self._status.set(f"已保存 PNG → {path}")

    # ════════════════════════════════════════════════════════════
    # 收尾
    # ════════════════════════════════════════════════════════════

    def on_close(self) -> None:
        """窗口关闭：收尾所有会话（与守护进程退出的语义一致）。

        同样走两阶段：先把会话全部摘除（列表立刻空），再等释放线程，最后才销毁窗口。
        """
        if self._tick_job is not None:  # 不取消的话，销毁后它还会触发一次并报错
            self._root.after_cancel(self._tick_job)
            self._tick_job = None
        self._closing.set()  # 让唤醒线程收工
        self._wake_thread.join(_WAKE_POLL * 2)
        self._runtime.close_all(timeout=_RELEASE_JOIN_SECONDS)
        self._root.destroy()


def main() -> int:
    root = tk.Tk()
    app = App(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()
    return 0
