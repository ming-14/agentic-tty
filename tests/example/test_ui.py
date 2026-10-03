"""共享控件（`example/ui/`）的冒烟测试：不弹窗口（`withdraw`），只验组件自身的行为。"""

from __future__ import annotations

import pytest

tk = pytest.importorskip("tkinter")
pytest.importorskip("resvg_py")  # 屏幕页的 SVG 栅格化依赖

from agentic_tty.example.ui import (  # noqa: E402
    DetailNotebook,
    Page,
    ScreenView,
    SessionTree,
    ViewRange,
    set_text,
    svg_size,
)

_SVG = '<svg width="640" height="408" viewBox="0 0 640 408"></svg>'


def test_svg_size_reads_the_rendered_root():
    assert svg_size(_SVG) == (640, 408)
    assert svg_size('<svg viewBox="0 0 640 408">') is None


def test_set_text_skips_identical_content(root):
    page = tk.Text(root)
    set_text(page, "hello")
    assert page.get("1.0", "end-1c") == "hello"
    set_text(page, "hello")  # 内容没变 → 不动
    assert page.get("1.0", "end-1c") == "hello"
    set_text(page, "next")
    assert page.get("1.0", "end-1c") == "next"


def test_detail_notebook_holds_pages_and_their_states(root):
    tabs = DetailNotebook(root)
    tabs.add_view_page(lambda: None)
    tabs.add_text(Page.RAW, "原始字节", small=True)

    assert tabs.view_mode.get() == ViewRange.SCREEN
    assert tabs.page_state(Page.RAW) == "normal"
    tabs.set_page_state(Page.RAW, "hidden")
    assert tabs.page_state(Page.RAW) == "hidden"
    tabs.set_text(Page.RAW, "hello")
    assert tabs.text(Page.RAW).get("1.0", "end-1c") == "hello"


def test_screen_view_repaints_only_when_the_key_changes(root):
    tabs = DetailNotebook(root)
    screen = ScreenView(tabs, on_format_change=lambda: None)
    painted: list[float] = []

    def produce(scale: float) -> tuple[bytes | None, str]:
        painted.append(scale)
        return None, ""

    assert screen.refresh(key=("u", 7), svg=_SVG, produce=produce) is None
    assert painted == [1.0]  # 画布还没量出尺寸 → 1×
    screen.refresh(key=("u", 7), svg=_SVG, produce=produce)
    assert painted == [1.0]  # 屏幕 / 偏移 / 格式都没变 → 不重画
    screen.refresh(key=("u", 8), svg=_SVG, produce=produce)
    assert painted == [1.0, 1.0]  # 输出偏移变了 → 重画
    assert screen.svg_source == _SVG


def test_screen_view_asks_the_caller_to_fetch_the_bitmap(root):
    """`image` 格式位图要走请求往返时，返回该取位图的缩放。"""
    tabs = DetailNotebook(root)
    screen = ScreenView(tabs, on_format_change=lambda: None)
    assert screen.refresh(key=("u", 7), svg=_SVG) == pytest.approx(1.0)
    assert screen.refresh(key=("u", 7), svg=_SVG) is None  # 要过一次就不再要


def test_changing_the_format_invalidates_the_cache(root):
    tabs = DetailNotebook(root)
    notified: list[bool] = []
    screen = ScreenView(tabs, on_format_change=lambda: notified.append(True))
    painted: list[float] = []

    def produce(scale: float) -> tuple[bytes | None, str]:
        painted.append(scale)
        return None, ""

    screen.refresh(key=("u", 7), svg=_SVG, produce=produce)
    screen.refresh(key=("u", 7), svg=_SVG, produce=produce)
    assert painted == [1.0]

    screen.format = "svg"
    screen.format = "image"
    assert notified == [True, True]
    screen.refresh(key=("u", 7), svg=_SVG, produce=produce)
    assert painted == [1.0, 1.0]  # 换过格式 → 缓存作废，重画一次


def test_screen_view_reset_clears_the_source(root):
    tabs = DetailNotebook(root)
    screen = ScreenView(tabs, on_format_change=lambda: None)
    screen.refresh(key=("u", 7), svg=_SVG, produce=lambda scale: (None, ""))
    screen.reset("未选中会话")
    assert screen.svg_source is None
    assert screen.note == "未选中会话"


def test_session_tree_refreshes_incrementally(root):
    picked: list[str | None] = []
    tree = SessionTree(root, on_select=picked.append)
    tree.refresh([("a", ("ls", "fake", "running", None, 2))])
    assert tree.values("a") == ("ls", "fake", "running", "-", "2")  # None → 占位符

    tree.refresh(
        [("a", ("ls", "fake", "running", None, 2)), ("b", ("cat", "pty", "exited", 0, None))]
    )
    tree.refresh([("b", ("cat", "pty", "exited", 0, None))])  # a 掉了
    assert tree.values("b") == ("cat", "pty", "exited", "0", "-")  # 0 是有效值，不是「观测不到」
    assert tree.selected() is None

    tree.set_selection("b")
    root.update()  # <<TreeviewSelect>> 是异步派发的
    assert tree.selected() == "b"
    assert picked == ["b"]
    tree.set_selection(None)
    root.update()
    assert tree.selected() is None
    assert picked == ["b", None]
