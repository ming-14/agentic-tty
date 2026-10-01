"""GUI 冒烟测试：不弹窗口（`withdraw`），只驱动几次 tick 验证接线正确。"""

from __future__ import annotations

import time

import pytest

tk = pytest.importorskip("tkinter")

from agentic_tty.core.ports import SessionMode, Stream  # noqa: E402
from agentic_tty.example.gui import App  # noqa: E402


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
    app._mode.set(SessionMode.PROCESS.value)
    app._create_session()
    assert len(app._sessions) == 1
    uid = next(iter(app._sessions))

    assert _pump_until(root, app, uid, lambda s: s.drained)
    session = app._sessions[uid]
    assert session.exit_code == 0
    assert b"build OK" in session.read_all(Stream.STDOUT)
    assert app._render_view(session).startswith("── stdout ──")
    app.on_close()


def test_send_input_and_close(root):
    app = App(root)
    app._command.set("repl")
    app._mode.set(SessionMode.PTY.value)
    app._create_session()
    uid = next(iter(app._sessions))

    app._input.insert(0, "hi")
    app._send_input(newline=True)
    assert _pump_until(root, app, uid, lambda s: b"echo: hi" in s.read_all(Stream.STDOUT))

    # 视图走的是 snapshot（Pty 基础服务）
    assert "echo: hi" in app._render_view(app._sessions[uid])

    app._selected = uid
    app._close_selected()
    assert app._sessions == {}
    app.on_close()


def test_resize_only_for_pty(root):
    app = App(root)
    app._command.set("tail")
    app._mode.set(SessionMode.PTY.value)
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
