"""守护进程验证台的冒烟测试：不起守护进程，只验「答复 → 界面」的翻译。"""

from __future__ import annotations

import pytest

pytest.importorskip("tkinter")
pytest.importorskip("resvg_py")  # 屏幕页的 SVG 栅格化依赖

from agentic_tty.example.daemon_test.gui import App
from agentic_tty.example.ui import Page

_SVG = '<svg width="640" height="408" viewBox="0 0 640 408"></svg>'


def test_command_box_is_plain_entry(root):
    """守护进程台没有假程序下拉，命令框是纯输入框。"""
    app = App(root)
    assert app._bar.mode.get() == "pty"
    assert app._bar.command.get() == ""
    app._bar.command.insert(0, "python -c pass")
    assert app._bar.command.get() == "python -c pass"
    app.on_close()


def test_tick_reports_the_daemon_as_not_started(root):
    app = App(root)
    app._tick()
    assert app._daemon_state.get() == "未启动"
    app.on_close()


def test_session_list_and_detail_land_on_the_widgets(root):
    app = App(root)
    app._selected = "t-1"  # 已经选着它，列表回来时把树的选中对齐过来
    app._apply_sessions(
        [
            {
                "sid": "t-1",
                "command": "cat",
                "mode": "pty",
                "state": "running",
                "exit_code": None,
                "members": 3,
            }
        ]
    )
    assert app._tree.values("t-1") == ("cat", "pty", "running", "-", "3")
    assert app._tree.selected() == "t-1"  # 选中对齐到新行

    app._apply_detail(
        {
            "sid": "t-1",
            "command": "cat",
            "mode": "pty",
            "state": "running",
            "drained": False,
            "exit_code": None,
            "is_terminal": True,
            "svg": _SVG,
            "offset": 7,
            "view": "屏幕文本",
            "raw": "原始字节",
            "procs": "进程清单",
            "cols": 100,
            "rows": 30,
            "title": "标题",
            "cwd": "/tmp",
        }
    )

    assert app._tabs.text(Page.VIEW).get("1.0", "end-1c") == "屏幕文本"
    assert app._tabs.text(Page.RAW).get("1.0", "end-1c") == "原始字节"
    assert app._tabs.text(Page.PROCS).get("1.0", "end-1c") == "进程清单"
    assert app._tabs.page_state(Page.SCREEN) == "normal"
    assert app._screen.svg_source == _SVG
    assert app._size.size == (100, 30)  # 只在换会话时填一次
    assert "disabled" not in app._size.apply_btn.state()
    assert "标题 · /tmp" in app._status.text
    app.on_close()


def test_zero_cells_are_not_blanked(root):
    """0 是有效值（退出码 0、0 个成员），只有观测不到才画 `-`。"""
    app = App(root)
    app._apply_sessions(
        [
            {
                "sid": "t-1",
                "command": "cat",
                "mode": "pty",
                "state": "exited",
                "exit_code": 0,
                "members": 0,
            },
            {
                "sid": "t-2",
                "command": "cat",
                "mode": "pty",
                "state": "running",
                "exit_code": None,
                "members": None,
            },
        ]
    )
    assert app._tree.values("t-1") == ("cat", "pty", "exited", "0", "0")
    assert app._tree.values("t-2") == ("cat", "pty", "running", "-", "-")
    app.on_close()


def test_process_session_hides_the_screen_pages(root):
    """屏幕页 / SVG 源码页 / 导出按钮 / 尺寸控件都是 pty 专属。"""
    app = App(root)
    app._selected = "t-1"
    app._apply_detail(
        {
            "sid": "t-1",
            "command": "cat",
            "mode": "subprocess",
            "state": "running",
            "is_terminal": False,
        }
    )

    assert app._tabs.page_state(Page.SCREEN) == "hidden"
    assert app._tabs.page_state(Page.SVG) == "hidden"
    assert app._screen.svg_source is None
    assert "没有屏幕" in app._screen.note
    for widget in (app._size.save_svg_btn, app._size.apply_btn, app._size.cols):
        assert "disabled" in widget.state()
    app.on_close()


def test_terminal_without_svg_reports_the_reason(root):
    """宿主给不出 SVG 时，屏幕页上要留一句原因。"""
    app = App(root)
    app._apply_detail({"sid": "t-1", "is_terminal": True, "svg": None, "svg_error": "宿主已关"})
    assert app._screen.svg_source is None
    assert "宿主已关" in app._screen.note
    assert app._tabs.page_state(Page.SCREEN) == "normal"
    app.on_close()


def test_selecting_nothing_clears_the_detail(root):
    app = App(root)
    app._selected = "t-1"
    app._apply_detail({"sid": "t-1", "is_terminal": True, "svg": _SVG, "view": "旧屏幕"})
    assert app._tabs.text(Page.VIEW).get("1.0", "end-1c") == "旧屏幕"

    app._select_session(None)  # 守护进程没在跑 → 直接清空

    assert app._tabs.text(Page.VIEW).get("1.0", "end-1c") == ""
    assert app._screen.svg_source is None
    assert app._tree.selected() is None
    app.on_close()
