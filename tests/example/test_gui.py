"""GUI 冒烟测试：不弹窗口（`withdraw`），只驱动几次 tick 验证接线正确。"""

from __future__ import annotations

import sys
import time

import pytest

tk = pytest.importorskip("tkinter")
pytest.importorskip("resvg_py")  # GUI 的 SVG 渲染依赖

from agentic_tty.core.ports import Stream  # noqa: E402
from agentic_tty.core.runtime.runner import SessionRunner  # noqa: E402
from agentic_tty.example.core_test import render  # noqa: E402
from agentic_tty.example.core_test.gui import App  # noqa: E402
from agentic_tty.example.core_test.sessions import ExampleMode, session_spec  # noqa: E402
from agentic_tty.example.ui import Page, ViewRange  # noqa: E402


def _pump_until(root: tk.Tk, app: App, uid: str, predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        root.update()
        if predicate(app._registry.get(uid)):
            return True
        time.sleep(0.01)
    return False


def test_create_session_pumps_to_completion(root):
    app = App(root)
    app._bar.command.set("build")
    app._bar.mode.set(ExampleMode.FAKE.value)
    app._create_session()
    sessions = app._registry.list()
    assert len(sessions) == 1
    uid = sessions[0].uid

    assert _pump_until(root, app, uid, lambda s: s.drained)
    session = app._registry.get(uid)
    assert session.exit_code == 0
    assert b"build OK" in session.read_all(Stream.STDOUT)
    app.on_close()


def test_send_input_and_close(root):
    app = App(root)
    app._bar.command.set("repl")
    app._bar.mode.set(ExampleMode.FAKE.value)
    app._create_session()
    uid = app._registry.list()[0].uid

    app._input.text = "hi"
    app._send_input(newline=True)
    assert _pump_until(root, app, uid, lambda s: b"echo: hi" in s.read_all(Stream.STDOUT))

    app._selected = uid
    app._close_selected()
    assert app._registry.list() == []
    app.on_close()


def test_mode_change_swaps_command_source(root):
    app = App(root)
    assert app._bar.mode.get() == ExampleMode.FAKE.value
    assert "repl" in app._bar.command["values"]

    app._bar.mode.set(ExampleMode.PTY.value)
    app._on_mode_change()
    assert not app._bar.command["values"]  # Tk 把空列表读回成 ""
    assert app._bar.command.get() == ""

    app._bar.mode.set(ExampleMode.FAKE.value)
    app._on_mode_change()
    assert "build" in app._bar.command["values"]
    app.on_close()


def test_pty_only_controls_hidden_for_fake(root):
    """屏幕页 / SVG 源码页 / 导出按钮 / 尺寸控件都是 pty 专属：其他模式藏掉或禁用。"""
    app = App(root)
    app._bar.command.set("repl")
    app._bar.mode.set(ExampleMode.FAKE.value)
    app._create_session()
    app._selected = app._registry.list()[0].uid
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
    assert app._screen.tab.grid_slaves(row=1) == [app._screen.canvas]
    app.on_close()


def test_process_tab_shows_tree_members(root):
    """「进程」页所有模式都有：会话树那列是成员数，页内列出成员 pid。"""
    app = App(root)
    app._bar.command.set("repl")
    app._bar.mode.set(ExampleMode.FAKE.value)
    app._create_session()
    uid = app._registry.list()[0].uid
    app._selected = uid

    # 假宿主的进程树成员由测试直接设（不接真进程）
    host = app._registry.get(uid).host
    assert host is not None
    host.descendants_pids = (101, 202)
    app._refresh_tree()
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
    app._registry = fake_registry
    session = fake_registry.create(session_spec(ExampleMode.SUBPROCESS, ("repl",)))
    app._selected = session.uid

    app._tabs.view_mode.set(ViewRange.FULL)
    app._refresh_detail()
    app._tabs.view_mode.set(ViewRange.SCREEN)
    app._refresh_detail()

    assert seen == [True, False]
    page = app._tabs.text(Page.VIEW)
    assert page.get("1.0", "end-1c").startswith("── stdout ──")
    fake_registry.close(session.uid)
    app.on_close()


def _pty_available() -> bool:
    try:
        from agentic_tty.core.runtime.pty_host import require_pywezterm

        require_pywezterm()
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _pty_available(), reason="pywezterm 不可用")
def test_screen_views_for_real_pty(root):
    """真 PTY：屏幕页 / SVG 源码页 / 尺寸控件可用，image 与 svg 都落成位图。"""
    app = App(root)
    session = app._registry.create(
        session_spec(ExampleMode.PTY, (sys.executable, "-c", "print('gui-svg')"))
    )
    runner = SessionRunner(session)
    runner.start()
    app._runners[session.uid] = runner
    app._selected = session.uid
    app._sync_pty_controls()

    assert _pump_until(root, app, session.uid, lambda s: s.drained)
    app._refresh_detail()

    assert app._tabs.page_state(Page.SCREEN) == "normal"
    assert "disabled" not in app._size.save_png_btn.state()
    assert "disabled" not in app._size.apply_btn.state()
    assert app._screen.svg_source.startswith("<svg") and "gui-svg" in app._screen.svg_source
    assert any(
        app._screen.canvas.type(i) == "image" for i in app._screen.canvas.find_all()
    )

    # 切到 svg 格式：走 resvg 栅格化，同样落成一张位图
    app._screen.format = "svg"
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
    app._registry = fake_registry
    sent: list[bytes] = []

    class _Spy:
        def submit_input(self, data: bytes) -> bool:
            sent.append(data)
            return True

        def stop(self, timeout: float = 2.0) -> None:
            pass

    for mode, expected in ((ExampleMode.PTY, b"hi\r"), (ExampleMode.SUBPROCESS, b"hi\n")):
        session = fake_registry.create(session_spec(mode, ("x",)))
        app._selected = session.uid
        app._runners[session.uid] = _Spy()  # type: ignore[assignment]
        app._input.text = "hi"
        app._send_input(newline=True)
        assert sent[-1] == expected
        fake_registry.close(session.uid)
    app.on_close()


def test_close_stdin_reaches_host(root, fake_registry):
    """「关 stdin」只对子进程有效，且真的传到宿主。"""
    app = App(root)
    app._registry = fake_registry
    session = fake_registry.create(session_spec(ExampleMode.SUBPROCESS, ("x",)))
    app._selected = session.uid
    app._close_stdin()
    assert session.host.stdin_closed
    fake_registry.close(session.uid)
    app.on_close()


def test_kill_keeps_the_session(root, fake_registry):
    """「强杀」只杀进程、保留会话——与「关闭选中」不同。"""
    app = App(root)
    app._registry = fake_registry
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
    app._registry = fake_registry
    session = fake_registry.create(session_spec(ExampleMode.SUBPROCESS, ("x",)))
    app._selected = session.uid
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
