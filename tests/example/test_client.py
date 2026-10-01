"""客户端界面冒烟：不弹窗口（`withdraw`），只驱动几次 tick 验证接线正确。

真正被验证的是**整条客户端路径**：发请求 → 收响应 → 收字节帧 → 渲染到画布。
"""

from __future__ import annotations

import time

import pytest

tk = pytest.importorskip("tkinter")

from agentic_tty.example import client as client_module  # noqa: E402
from agentic_tty.example.client import _TAB_SCREEN, ClientApp  # noqa: E402

from .wire import WireClient  # noqa: E402


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


def _drive(root: tk.Tk, seconds: float) -> None:
    """跑一会儿真实的 after 调度。"""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        root.update()
        time.sleep(0.005)


def _drive_until_photo(root: tk.Tk, app: ClientApp, timeout: float = 30.0) -> bool:
    """等到屏幕位图画出来。

    位图渲染是守护进程里最重的一次调用，可能远慢于 200ms 的刷新步长；等待期间
    每秒主动补发一次刷新，别把成败押在某个 tick 恰好落在渲染完成之后。
    """
    deadline = time.monotonic() + timeout
    next_request = 0.0
    while time.monotonic() < deadline:
        if app._photo is not None:
            return True
        now = time.monotonic()
        if now >= next_request:
            app._refresh()
            next_request = now + 1.0
        _drive(root, 0.05)
    return app._photo is not None


def _drive_until(root: tk.Tk, predicate, timeout: float = 20.0) -> bool:
    """跑到条件成立为止。

    超时给得宽：这里跑的是真 Tk + 真守护进程 + 真 PTY，全量测试时机器上有别的负载，
    200ms 的刷新节奏被拉长很正常。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        _drive(root, 0.05)
    return predicate()


def test_endpoint_uri_reads_the_rendezvous_file(tmp_path, monkeypatch):
    monkeypatch.setattr(client_module, "default_runtime_dir", lambda name: tmp_path / name)
    (tmp_path / "demo").mkdir()
    (tmp_path / "demo" / "endpoint").write_text("tcp://127.0.0.1:9\n", encoding="utf-8")
    assert client_module._endpoint_uri("demo") == "tcp://127.0.0.1:9"


def test_endpoint_uri_is_empty_when_there_is_no_daemon(tmp_path, monkeypatch):
    monkeypatch.setattr(client_module, "default_runtime_dir", lambda name: tmp_path / name)
    assert client_module._endpoint_uri("nope") == ""


def test_connect_create_and_render_the_screen(root, demo_daemon):
    app = ClientApp(root, uri=demo_daemon.address, name="unused")
    try:
        app._connect()
        assert app._channel is not None, f"连不上: {app._status.get()}"
        app._create()  # 命令框留空 = 起 shell

        assert _drive_until(root, lambda: app._selected in app._sessions), (
            f"会话没出现在列表里: {app._status.get()}"
        )
        # 屏幕页是默认页：位图由守护进程渲染好、经字节帧送过来，客户端只负责显示
        assert _drive_until_photo(root, app), f"屏幕位图没渲染出来: {app._status.get()}"
    finally:
        app.on_close()
    assert app._channel is None


def test_input_travels_and_shows_up_on_screen(root, demo_daemon):
    app = ClientApp(root, uri=demo_daemon.address, name="unused")
    try:
        app._connect()
        app._create()
        assert _drive_until(root, lambda: app._selected in app._sessions)
        app._notebook.select(app._texts[_TAB_SCREEN])
        app._input.insert(0, "echo hi-marker")
        app._send_text(newline=True)
        widget = app._texts[_TAB_SCREEN]
        assert _drive_until(root, lambda: "hi-marker" in widget.get("1.0", "end-1c"))
    finally:
        app.on_close()


def test_closing_the_window_only_disconnects(root, demo_daemon):
    """关窗只是断开连接——**会话照常活着**。"""
    app = ClientApp(root, uri=demo_daemon.address, name="unused")
    app._connect()
    app._create()
    assert _drive_until(root, lambda: app._selected in app._sessions)
    sid = app._selected
    app.on_close()

    probe = WireClient(demo_daemon.address)
    try:
        sids = [s["sid"] for s in probe.request("list_sessions").payload.output["data"]["sessions"]]
        assert sid in sids
    finally:
        probe.close()
