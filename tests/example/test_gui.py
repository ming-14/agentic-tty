"""GUI 冒烟测试：不弹窗口（`withdraw`），只驱动几次 tick 验证接线正确。"""

from __future__ import annotations

import sys
import time

import pytest

tk = pytest.importorskip("tkinter")
pytest.importorskip("resvg_py")  # GUI 的 SVG 渲染依赖

from agentic_tty.core.ports import Stream  # noqa: E402
from agentic_tty.core.runtime.input_queue import InputVerdict  # noqa: E402
from agentic_tty.core.runtime.runtime import Runtime  # noqa: E402
from agentic_tty.core.terminal.session import TerminalSession  # noqa: E402
from agentic_tty.example.core_test_console import render  # noqa: E402
from agentic_tty.example.core_test_console.gui import App  # noqa: E402
from agentic_tty.example.core_test_console.sessions import (  # noqa: E402
    ExampleMode,
    make_runner_factory,
    session_spec,
)
from agentic_tty.example.ui import FORMAT_IMAGE, FORMAT_SVG, Page, ViewRange  # noqa: E402

_SVG = '<svg width="640" height="408" viewBox="0 0 640 408"></svg>'


def _pump_until(root: tk.Tk, app: App, uid: str, predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        root.update()
        if predicate(app._runtime.get(uid)):
            return True
        time.sleep(0.01)
    return False


def test_create_session_pumps_to_completion(root):
    app = App(root)
    app._bar.command.set("build")
    app._bar.mode.set(ExampleMode.FAKE.value)
    app._create_session()
    sessions = app._runtime.list()
    assert len(sessions) == 1
    uid = sessions[0].uid

    assert _pump_until(root, app, uid, lambda s: s.drained)
    session = app._runtime.get(uid)
    assert session.exit_code == 0
    assert b"build OK" in session.read_all(Stream.STDOUT)
    app.on_close()


def test_create_session_uses_the_working_directory_row(root, fake_registry, tmp_path):
    """工作目录行的值进 `SessionSpec.cwd`；留空就是 `None`（宿主退到当前目录）。

    `sandbox_pty` 下这个字段同时就是可写区，所以它必须真的到位，不能只存在界面上。
    """
    app = App(root)
    app._runtime = Runtime(fake_registry)
    app._bar.mode.set(ExampleMode.PTY.value)
    app._bar.command.set("x")

    app._dir.set_directory(str(tmp_path))
    app._create_session()
    app._dir.set_directory(None)
    app._create_session()

    assert [session.spec.cwd for session in app._runtime.list()] == [str(tmp_path), None]
    app.on_close()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows 路径的反斜杠")
def test_command_box_keeps_backslashes(root, fake_registry):
    """命令框里填的 Windows 路径原样进 `argv`——反斜杠是路径分隔符，不是转义符。"""
    app = App(root)
    app._runtime = Runtime(fake_registry)
    app._bar.mode.set(ExampleMode.PTY.value)
    app._bar.command.set(r"C:\Windows\System32\cmd.exe /c dir")
    app._create_session()

    assert app._runtime.list()[0].spec.argv == (r"C:\Windows\System32\cmd.exe", "/c", "dir")
    app.on_close()


def test_send_input_and_close(root):
    app = App(root)
    app._bar.command.set("repl")
    app._bar.mode.set(ExampleMode.FAKE.value)
    app._create_session()
    uid = app._runtime.list()[0].uid

    app._input.text = "hi"
    app._send_input(newline=True)
    assert _pump_until(root, app, uid, lambda s: b"echo: hi" in s.read_all(Stream.STDOUT))

    app._selected = uid
    app._close_selected()
    assert app._runtime.list() == []
    app.on_close()


def test_opens_on_fake_with_a_default_command(root):
    """打开台子就能直接点「创建会话」：命令框的初值跟着初始模式摆。"""
    app = App(root)
    assert app._bar.mode.get() == ExampleMode.FAKE.value
    assert app._bar.command.get() == "repl"
    app.on_close()


def test_mode_change_swaps_command_source(root):
    app = App(root)
    assert app._bar.mode.get() == ExampleMode.FAKE.value
    assert "repl" in app._bar.command["values"]

    app._bar.mode.set(ExampleMode.PTY.value)
    app._sync_command_box()
    assert not app._bar.command["values"]  # Tk 把空列表读回成 ""
    assert app._bar.command.get() == ""

    app._bar.mode.set(ExampleMode.FAKE.value)
    app._sync_command_box()
    assert "build" in app._bar.command["values"]
    app.on_close()


def test_pty_only_controls_hidden_for_fake(root):
    """屏幕页 / SVG 源码页 / 导出按钮 / 尺寸控件都是 pty 专属：其他模式藏掉或禁用。"""
    app = App(root)
    app._bar.command.set("repl")
    app._bar.mode.set(ExampleMode.FAKE.value)
    app._create_session()
    app._selected = app._runtime.list()[0].uid
    app._refresh_detail()

    assert app._tabs.page_state(Page.SCREEN) == "hidden"
    assert app._tabs.page_state(Page.SVG) == "hidden"
    assert app._screen.svg_source is None
    for widget in (
        app._size.save_svg_btn,
        app._size.save_png_btn,
        app._size.apply_btn,
        app._size.cols,
        app._size.rows,
    ):
        assert "disabled" in widget.state()
    assert not any(
        app._screen.canvas.type(i) == "image" for i in app._screen.canvas.find_all()
    )
    app.on_close()


def test_screen_image_fits_canvas(root):
    """屏幕按画布大小缩放铺满，画布独占一行（没有滚动条）。"""
    app = App(root)
    app._screen.canvas.winfo_width = lambda: 600
    app._screen.canvas.winfo_height = lambda: 300

    # 1.0 倍的尺寸由渲染结果自带：80×24 字符 → 640×408。按较小的一维贴合画布。
    assert app._screen.fit_scale((640, 408)) == pytest.approx(min(600 / 640, 300 / 408))
    # 格式单选占第 0 行，画布独占第 1 行（没有滚动条）
    assert app._screen.tab.grid_slaves(row=1) == [app._screen.canvas]
    app.on_close()


def test_size_box_follows_the_selected_session(root, fake_registry):
    """尺寸框跟着会话走——留着上一个会话的宽高，点「应用」会改到别的会话头上。"""
    app = App(root)
    app._runtime = Runtime(fake_registry)
    first = fake_registry.create(session_spec(ExampleMode.PTY, ("a",)))
    second = fake_registry.create(session_spec(ExampleMode.PTY, ("b",)))
    app._refresh_tree()

    app._select_session(first.uid)
    app._size.set_size(100, 30)
    app._resize()
    assert (first.cols, first.rows) == (100, 30)

    app._select_session(second.uid)
    assert app._size.size == (second.cols, second.rows)  # 不再是上一个会话的 100×30
    fake_registry.close(first.uid)
    fake_registry.close(second.uid)
    app.on_close()


def test_process_tab_shows_tree_members(root):
    """「进程」页所有模式都有：会话树那列是成员数，页内列出成员 pid。"""
    app = App(root)
    app._bar.command.set("repl")
    app._bar.mode.set(ExampleMode.FAKE.value)
    app._create_session()
    uid = app._runtime.list()[0].uid
    app._selected = uid

    # 假宿主的进程树成员由测试直接设（不接真进程）
    host = app._runtime.get(uid).host
    assert host is not None
    host.descendants_pids = (101, 202)
    app._refresh_tree()
    app._tabs.show_page(Page.PROCS)  # 详情区只刷看得见的那一页
    app._refresh_detail()

    assert app._tabs.page_state(Page.PROCS) == "normal"  # 不是 pty 专属
    assert app._tree.values(uid)[4] == "2"
    text = app._tabs.text(Page.PROCS).get("1.0", "end-1c")
    assert "进程树成员 2 个" in text
    assert "pid 101" in text and "pid 202" in text
    app.on_close()


def test_view_page_reads_its_range(root, fake_registry, monkeypatch):
    """「视图」页的范围单选直接决定喂给 core 的是哪一项返回数据。"""
    seen: list[bool] = []
    original = render.view_text

    def spy(session, *, full: bool) -> str:
        seen.append(full)
        return original(session, full=full)

    monkeypatch.setattr(render, "view_text", spy)

    app = App(root)
    app._runtime = Runtime(fake_registry)
    session = fake_registry.create(session_spec(ExampleMode.SUBPROCESS, ("repl",)))
    app._selected = session.uid

    app._tabs.show_page(Page.VIEW)
    app._tabs.view_mode.set(ViewRange.FULL)
    app._refresh_detail()
    app._tabs.view_mode.set(ViewRange.SCREEN)
    app._refresh_detail()

    assert seen == [True, False]
    page = app._tabs.text(Page.VIEW)
    assert page.get("1.0", "end-1c").startswith("── stdout ──")
    fake_registry.close(session.uid)
    app.on_close()


def test_selecting_a_session_keeps_the_current_page(root, fake_registry, monkeypatch):
    """选中会话**不切页**：用户看哪页就留在哪页。

    「屏幕」页默认 `image` 格式（走 `render_image`），一被切过去就要把整个字体库拉进
    内存（约 650 MB）——选中一个会话不该顺手把位图渲染出来。
    """
    rendered: list[float] = []
    monkeypatch.setattr(
        render, "screen_png", lambda session, scale: (rendered.append(scale), b"")[1]
    )

    app = App(root)
    app._runtime = Runtime(fake_registry)
    app._tabs.show_page(Page.VIEW)

    session = fake_registry.create(session_spec(ExampleMode.PTY, ("x",)))
    app._refresh_tree()
    app._select_session(session.uid)

    assert app._tabs.current is Page.VIEW
    assert rendered == []
    fake_registry.close(session.uid)
    app.on_close()


def test_only_the_visible_page_is_rendered(root, fake_registry, monkeypatch):
    """看哪页取哪页：屏幕页不可见时，一次栅格化都不该发生。"""
    painted: list[float] = []
    monkeypatch.setattr(TerminalSession, "render_svg", lambda self: _SVG)

    app = App(root)
    app._runtime = Runtime(fake_registry)

    def fake_rasterize(svg: str, scale: float) -> tuple[bytes | None, str]:
        painted.append(scale)
        return None, ""

    app._screen.rasterize = fake_rasterize  # type: ignore[method-assign]
    session = fake_registry.create(session_spec(ExampleMode.PTY, ("x",)))
    app._selected = session.uid
    app._sync_pty_controls()

    app._tabs.show_page(Page.VIEW)
    app._screen.format = FORMAT_SVG  # 用本地栅格化计数（image 格式走 produce）
    app._refresh_detail()
    assert painted == []  # 看「视图」页 → 屏幕一次都没碰

    app._tabs.show_page(Page.SCREEN)
    app._refresh_detail()
    assert painted == [1.0]  # 切到「屏幕」页才栅格化

    app._refresh_detail()
    assert painted == [1.0]  # 同一份矢量 → 不重复栅格化

    app._tabs.show_page(Page.SVG)
    app._refresh_detail()
    assert painted == [1.0]  # 「SVG 源码」页只要文本，不栅格化
    assert app._screen.svg_source == _SVG

    fake_registry.close(session.uid)
    app.on_close()


def test_screen_format_switch_picks_the_bitmap_source(root, fake_registry, monkeypatch):
    """「屏幕」页的格式单选：`image` 走模型直接出位图，`svg` 走本地栅格化。"""
    painted: list[float] = []
    monkeypatch.setattr(TerminalSession, "render_svg", lambda self: _SVG)

    app = App(root)
    app._runtime = Runtime(fake_registry)

    def fake_rasterize(svg: str, scale: float) -> tuple[bytes | None, str]:
        painted.append(scale)
        return None, ""

    app._screen.rasterize = fake_rasterize  # type: ignore[method-assign]
    session = fake_registry.create(session_spec(ExampleMode.PTY, ("x",)))
    app._selected = session.uid
    app._sync_pty_controls()
    app._tabs.show_page(Page.SCREEN)

    app._screen.format = FORMAT_SVG
    app._refresh_detail()
    assert painted == [1.0]  # svg → 本地 resvg

    app._screen.format = FORMAT_IMAGE
    app._refresh_detail()
    assert painted == [1.0]  # image → 走 produce，不碰 resvg
    # 假宿主出不了位图 → 画布上留一行提示（`set_image` 画的是画布，不是 `note`）
    assert any(
        app._screen.canvas.type(i) == "text" for i in app._screen.canvas.find_all()
    )

    fake_registry.close(session.uid)
    app.on_close()


def _pty_available() -> bool:
    try:
        from agentic_tty.core.runtime.pywezterm_pty.host import require_pywezterm

        require_pywezterm()
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _pty_available(), reason="pywezterm 不可用")
def test_screen_views_for_real_pty(root):
    """真 PTY：屏幕页 / SVG 源码页 / 尺寸控件可用，矢量在本地栅格化成位图。"""
    app = App(root)
    session = app._runtime.create(
        session_spec(ExampleMode.PTY, (sys.executable, "-c", "print('gui-svg')"))
    )
    app._selected = session.uid
    app._sync_pty_controls()
    app._tabs.show_page(Page.SCREEN)  # 选中会话不切页，屏幕页要自己切过去

    # 真起进程 + 真 PTY，满载时会慢——预算给宽一点，别把它当断言失败
    assert _pump_until(root, app, session.uid, lambda s: s.drained, timeout=20.0)
    app._refresh_detail()

    assert app._tabs.page_state(Page.SCREEN) == "normal"
    assert "disabled" not in app._size.save_png_btn.state()
    assert "disabled" not in app._size.apply_btn.state()
    assert app._screen.svg_source.startswith("<svg") and "gui-svg" in app._screen.svg_source
    assert any(
        app._screen.canvas.type(i) == "image" for i in app._screen.canvas.find_all()
    )

    app._size.set_size(100, 30)
    app._resize()
    assert (session.cols, session.rows) == (100, 30)
    app.on_close()


def test_send_input_encodes_newline_per_mode(root, fake_registry):
    """回车编码由消费者做：PTY 是 CR、子进程是 LF（core 只收字节）。"""
    app = App(root)
    app._runtime = Runtime(fake_registry)
    sent: list[bytes] = []

    class _Spy:
        def submit_input(self, data: bytes) -> InputVerdict:
            sent.append(data)
            return InputVerdict.QUEUED

        def stop(self, timeout: float = 2.0) -> None:
            pass

    for mode, expected in ((ExampleMode.PTY, b"hi\r"), (ExampleMode.SUBPROCESS, b"hi\n")):
        session = fake_registry.create(session_spec(mode, ("x",)))
        app._selected = session.uid
        # 替掉写线程的驱动，只看"交给写线程的字节"——不必等真线程
        app._runtime._runners[session.uid] = _Spy()  # type: ignore[assignment]
        app._input.text = "hi"
        app._send_input(newline=True)
        assert sent[-1] == expected
        fake_registry.close(session.uid)
    app.on_close()


def test_close_stdin_reaches_host(root, fake_registry):
    """「关 stdin」只对子进程有效，且真的传到宿主。"""
    app = App(root)
    app._runtime = Runtime(fake_registry)
    session = fake_registry.create(session_spec(ExampleMode.SUBPROCESS, ("x",)))
    app._selected = session.uid
    app._close_stdin()
    assert session.host.stdin_closed
    fake_registry.close(session.uid)
    app.on_close()


def test_kill_keeps_the_session(root, fake_registry):
    """「强杀」只杀进程、保留会话——与「关闭选中」不同。"""
    app = App(root)
    app._runtime = Runtime(fake_registry)
    session = fake_registry.create(session_spec(ExampleMode.SUBPROCESS, ("x",)))
    app._selected = session.uid
    app._kill_selected()
    assert session.exit_code is not None  # 假宿主强杀后立刻可见
    assert fake_registry.find(session.uid) is session  # 仍在表里
    fake_registry.close(session.uid)
    app.on_close()


def test_subscription_page_collects_new_output(root, fake_registry):
    """「订阅流」页用 core 的 `Subscription` 按游标取增量（订阅机制的用法）。"""
    app = App(root)
    app._runtime = Runtime(fake_registry)
    session = fake_registry.create(session_spec(ExampleMode.SUBPROCESS, ("x",)))
    app._selected = session.uid
    app._tabs.show_page(Page.SUB)
    app._refresh_detail()  # 建订阅：游标从当前末尾起
    page = app._tabs.text(Page.SUB)
    assert page.get("1.0", "end-1c").startswith("── 订阅自 offset")

    session.ingest_stream(Stream.STDOUT, b"hello")
    app._refresh_detail()  # 拉增量
    assert "hello" in page.get("1.0", "end-1c")
    assert app._subscription is not None
    assert app._subscription.next_offset == session.journal.end_offset
    fake_registry.close(session.uid)
    app.on_close()


def test_flood_input_reports_rejection(root, fake_registry):
    """「灌满输入队列」：整块拒收，判定写在状态栏——绝不静默丢字节。"""
    app = App(root)
    # 台子的驱动带**小水位**（示例层的装配），否则 4KB 撞不到 1 MiB 的默认硬上限
    app._runtime = Runtime(fake_registry, runner_factory=make_runner_factory())
    session = app._runtime.create(session_spec(ExampleMode.SUBPROCESS, ("x",)))
    app._selected = session.uid

    app._flood_input()
    assert "rejected" in app._status.text
    app.on_close()
