"""GUI 冒烟测试：不弹窗口（`withdraw`），只驱动几次 tick 验证接线正确。"""

from __future__ import annotations

import sys
import time

import pytest

tk = pytest.importorskip("tkinter")
pytest.importorskip("resvg_py")  # GUI 的 SVG 渲染依赖

from agentic_tty.core.ports import Stream  # noqa: E402
from agentic_tty.example.gui import App  # noqa: E402
from agentic_tty.example.sessions import ExampleMode, create_session  # noqa: E402
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
        if predicate(app._sessions[uid]):
            return True
        time.sleep(0.01)
    return False


def test_create_session_pumps_to_completion(root):
    app = App(root)
    app._command.set("build")
    app._mode.set(ExampleMode.FAKE.value)
    app._create_session()
    assert len(app._sessions) == 1
    uid = next(iter(app._sessions))

    assert _pump_until(root, app, uid, lambda s: s.drained)
    session = app._sessions[uid]
    assert session.exit_code == 0
    assert b"build OK" in session.read_all(Stream.STDOUT)
    assert app._render_view(session).startswith("── 屏幕 ──")
    app.on_close()


def test_send_input_and_close(root):
    app = App(root)
    app._command.set("repl")
    app._mode.set(ExampleMode.FAKE.value)
    app._create_session()
    uid = next(iter(app._sessions))

    app._input.insert(0, "hi")
    app._send_input(newline=True)
    assert _pump_until(root, app, uid, lambda s: b"echo: hi" in s.read_all(Stream.STDOUT))

    # 假会话的视图含屏幕快照
    assert "echo: hi" in app._render_view(app._sessions[uid])

    app._selected = uid
    app._close_selected()
    assert app._sessions == {}
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


def test_resize_fake_session(root):
    app = App(root)
    app._command.set("tail")
    app._mode.set(ExampleMode.FAKE.value)
    app._create_session()
    uid = next(iter(app._sessions))
    app._selected = uid
    app._cols.delete(0, "end")
    app._cols.insert(0, "100")
    app._rows.delete(0, "end")
    app._rows.insert(0, "30")
    app._resize()
    assert (app._sessions[uid].cols, app._sessions[uid].rows) == (100, 30)
    app.on_close()


def test_screen_views_for_fake_session(root):
    """假宿主给最小 SVG；位图不支持时屏幕页给提示，而不是崩。"""
    app = App(root)
    app._command.set("repl")
    app._mode.set(ExampleMode.FAKE.value)
    app._create_session()
    app._selected = next(iter(app._sessions))
    app._refresh_detail()

    assert app._svg_source.startswith("<svg")
    assert not any(app._image_canvas.type(i) == "image" for i in app._image_canvas.find_all())
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
    """真 PTY：屏幕页拿到位图，SVG 源码页拿到矢量源码。"""
    app = App(root)
    session = create_session(ExampleMode.PTY, (sys.executable, "-c", "print('gui-svg')"))
    session.start()
    runner = SessionRunner(session)
    runner.start()
    app._sessions[session.uid] = session
    app._runners[session.uid] = runner
    app._modes[session.uid] = ExampleMode.PTY
    app._selected = session.uid

    assert _pump_until(root, app, session.uid, lambda s: s.drained)
    app._refresh_detail()

    assert app._svg_source.startswith("<svg") and "gui-svg" in app._svg_source
    assert any(app._image_canvas.type(i) == "image" for i in app._image_canvas.find_all())

    # 切到 svg 格式：走 resvg 栅格化，同样落成一张位图
    app._format.set("svg")
    app._refresh_detail()
    assert any(app._image_canvas.type(i) == "image" for i in app._image_canvas.find_all())
    app.on_close()
