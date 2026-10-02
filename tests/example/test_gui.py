"""GUI 冒烟测试：不弹窗口（`withdraw`），只驱动几次 tick 验证接线正确。"""

from __future__ import annotations

import sys
import time

import pytest

tk = pytest.importorskip("tkinter")
pytest.importorskip("resvg_py")  # GUI 的 SVG 渲染依赖

from agentic_tty.core.ports import Stream  # noqa: E402
from agentic_tty.example.core_test.gui import App  # noqa: E402
from agentic_tty.example.core_test.sessions import ExampleMode, session_spec  # noqa: E402
from agentic_tty.runtime.runner import SessionRunner  # noqa: E402


@pytest.fixture
def root():
    try:
        widget = tk.Tk()
    except tk.TclError as exc:  # 无显示环境
        pytest.skip(f"无法创建 Tk 窗口: {exc}")
    widget.withdraw()
    yield widget
    try:
        widget.destroy()
    except tk.TclError:
        pass


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
    app._command.set("build")
    app._mode.set(ExampleMode.FAKE.value)
    app._create_session()
    sessions = app._registry.list()
    assert len(sessions) == 1
    uid = sessions[0].uid

    assert _pump_until(root, app, uid, lambda s: s.drained)
    session = app._registry.get(uid)
    assert session.exit_code == 0
    assert b"build OK" in session.read_all(Stream.STDOUT)
    assert app._render_view(session).startswith("── stdout ──")
    app.on_close()


def test_send_input_and_close(root):
    app = App(root)
    app._command.set("repl")
    app._mode.set(ExampleMode.FAKE.value)
    app._create_session()
    uid = app._registry.list()[0].uid

    app._input.insert(0, "hi")
    app._send_input(newline=True)
    assert _pump_until(root, app, uid, lambda s: b"echo: hi" in s.read_all(Stream.STDOUT))

    # 假会话没有屏幕视图，视图就是双流
    assert "echo: hi" in app._render_view(app._registry.get(uid))

    app._selected = uid
    app._close_selected()
    assert app._registry.list() == []
    app.on_close()


def test_mode_change_swaps_command_source(root):
    app = App(root)
    assert app._mode.get() == ExampleMode.FAKE.value
    assert "repl" in app._command["values"]

    app._mode.set(ExampleMode.PTY.value)
    app._on_mode_change()
    assert not app._command["values"]  # Tk 把空列表读回成 ""
    assert app._command.get() == ""

    app._mode.set(ExampleMode.FAKE.value)
    app._on_mode_change()
    assert "build" in app._command["values"]
    app.on_close()


def test_pty_only_controls_hidden_for_fake(root):
    """屏幕页 / SVG 源码页 / 导出按钮 / 尺寸控件都是 pty 专属：其他模式藏掉或禁用。"""
    app = App(root)
    app._command.set("repl")
    app._mode.set(ExampleMode.FAKE.value)
    app._create_session()
    app._selected = app._registry.list()[0].uid
    app._refresh_detail()

    assert app._notebook.tab(app._image_tab, "state") == "hidden"
    assert app._notebook.tab(app._svg, "state") == "hidden"
    assert app._svg_source is None
    for widget in (app._save_svg_btn, app._save_png_btn, app._resize_btn, app._cols, app._rows):
        assert "disabled" in widget.state()
    assert not any(app._image_canvas.type(i) == "image" for i in app._image_canvas.find_all())
    app.on_close()


def test_screen_image_fits_canvas(root):
    """屏幕按画布大小缩放铺满，画布独占一行（没有滚动条）。"""
    app = App(root)
    app._image_canvas.winfo_width = lambda: 600
    app._image_canvas.winfo_height = lambda: 300

    # 1.0 倍的尺寸由渲染结果自带：80×24 字符 → 640×408。按较小的一维贴合画布。
    assert app._fit_scale((640, 408)) == pytest.approx(min(600 / 640, 300 / 408))
    assert app._image_tab.grid_slaves(row=1) == [app._image_canvas]
    app.on_close()


def test_process_tab_shows_tree_members(root):
    """「进程」页所有模式都有：会话树那列是成员数，页内列出成员 pid。"""
    app = App(root)
    app._command.set("repl")
    app._mode.set(ExampleMode.FAKE.value)
    app._create_session()
    uid = app._registry.list()[0].uid
    app._selected = uid

    # 假宿主的进程树成员由测试直接设（不接真进程）
    host = app._registry.get(uid).host
    assert host is not None
    host.descendants_pids = (101, 202)
    app._refresh_tree()
    app._refresh_detail()

    assert app._notebook.tab(app._procs, "state") == "normal"  # 不是 pty 专属
    assert app._tree.item(uid, "values")[4] == "2"
    text = app._procs.get("1.0", "end-1c")
    assert "进程树成员 2 个" in text
    assert "pid 101" in text and "pid 202" in text
    app.on_close()


def test_processes_helper_returns_none_when_unobservable(root):
    """未启动的会话观测不到进程树：返回 None 而不是抛给界面。"""
    app = App(root)
    session = app._registry.create(session_spec(ExampleMode.FAKE, ("repl",)))  # 没 start
    assert app._processes(session) is None
    app.on_close()


def _pty_available() -> bool:
    try:
        from agentic_tty.runtime.pty_host import require_pywezterm

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
    session.start()
    runner = SessionRunner(session)
    runner.start()
    app._runners[session.uid] = runner
    app._selected = session.uid
    app._sync_pty_controls()

    assert _pump_until(root, app, session.uid, lambda s: s.drained)
    app._refresh_detail()

    assert app._notebook.tab(app._image_tab, "state") == "normal"
    assert "disabled" not in app._save_png_btn.state()
    assert "disabled" not in app._resize_btn.state()
    assert app._svg_source.startswith("<svg") and "gui-svg" in app._svg_source
    assert any(app._image_canvas.type(i) == "image" for i in app._image_canvas.find_all())

    # 切到 svg 格式：走 resvg 栅格化，同样落成一张位图
    app._format.set("svg")
    app._refresh_detail()
    assert any(app._image_canvas.type(i) == "image" for i in app._image_canvas.find_all())

    app._cols.delete(0, "end")
    app._cols.insert(0, "100")
    app._rows.delete(0, "end")
    app._rows.insert(0, "30")
    app._resize()
    assert (session.cols, session.rows) == (100, 30)
    app.on_close()
