"""共享控件（`example/ui/`）的冒烟测试：不弹窗口（`withdraw`），只验组件自身的行为。"""

from __future__ import annotations

import sys

import pytest

tk = pytest.importorskip("tkinter")
pytest.importorskip("resvg_py")  # 屏幕页的 SVG 栅格化依赖

from tkinter import ttk  # noqa: E402

from agentic_tty.example.ui import (  # noqa: E402
    FORMAT_IMAGE,
    FORMAT_SVG,
    DetailNotebook,
    DirBar,
    Page,
    ScreenView,
    SessionTree,
    ViewRange,
    bars,
    set_text,
    split_command,
    svg_size,
)

_SVG = '<svg width="640" height="408" viewBox="0 0 640 408"></svg>'
_SVG_OTHER = '<svg width="320" height="204" viewBox="0 0 320 204"></svg>'


def test_svg_size_reads_the_rendered_root():
    assert svg_size(_SVG) == (640, 408)
    assert svg_size('<svg viewBox="0 0 640 408">') is None


def test_split_command_splits_on_whitespace():
    """普通命令行按空白拆；双引号里的空格不断开——两个平台都是这么拆的。"""
    assert split_command("cmd.exe /c dir") == ("cmd.exe", "/c", "dir")
    assert split_command(r'"C:\Program Files\app.exe" /c dir') == (
        r"C:\Program Files\app.exe",
        "/c",
        "dir",
    )
    assert split_command("") == ()
    assert split_command("   ") == ()


@pytest.mark.skipif(sys.platform != "win32", reason="只有 Windows 的反斜杠才是路径分隔符")
def test_split_command_keeps_windows_paths_whole():
    """裸的 Windows 路径必须原样保留。

    POSIX 的 `shlex` 把 `\\` 当转义符，会把它啃成 `C:WindowsSystem32cmd.exe`——进程起不来。
    """
    assert split_command(r"C:\Windows\System32\cmd.exe") == (r"C:\Windows\System32\cmd.exe",)
    assert split_command(r"C:\Windows\System32\cmd.exe /c dir") == (
        r"C:\Windows\System32\cmd.exe",
        "/c",
        "dir",
    )


def test_set_text_skips_identical_content(root):
    page = tk.Text(root)
    set_text(page, "hello")
    assert page.get("1.0", "end-1c") == "hello"
    set_text(page, "hello")  # 内容没变 → 不动
    assert page.get("1.0", "end-1c") == "hello"
    set_text(page, "next")
    assert page.get("1.0", "end-1c") == "next"


def _browse_button(bar: DirBar) -> ttk.Button:
    return next(w for w in bar.winfo_children() if isinstance(w, ttk.Button))


def test_dir_bar_keeps_what_the_picker_returned(root, monkeypatch):
    """工作目录行：留空 = 默认目录（`None`），选中的目录原样留着，取消不改动。"""
    bar = DirBar(root)
    assert bar.directory is None

    monkeypatch.setattr(bars, "ask_directory", lambda **_kwargs: "/picked/dir")
    _browse_button(bar).invoke()
    assert bar.directory == "/picked/dir"

    monkeypatch.setattr(bars, "ask_directory", lambda **_kwargs: "")
    _browse_button(bar).invoke()
    assert bar.directory == "/picked/dir"

    bar.set_directory("   ")
    assert bar.directory is None  # 只剩空白也算留空


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


def test_screen_view_repaints_only_when_the_svg_changes(root):
    """栅格化最贵，所以拿矢量本身当指纹：同一份矢量不重画。"""
    tabs = DetailNotebook(root)
    screen = ScreenView(tabs)
    painted: list[float] = []

    def fake_rasterize(svg: str, scale: float) -> tuple[bytes | None, str]:
        painted.append(scale)
        return None, ""

    screen.rasterize = fake_rasterize  # type: ignore[method-assign]

    screen.show_screen(_SVG)
    assert painted == [1.0]  # 画布还没量出尺寸 → 1×
    screen.show_screen(_SVG)
    assert painted == [1.0]  # 矢量没变 → 不重画
    screen.show_screen(_SVG_OTHER)
    assert painted == [1.0, 1.0]  # 内容变了 → 重画
    assert screen.svg_source == _SVG_OTHER


def test_screen_source_page_never_rasterizes(root):
    """「SVG 源码」页只要文本——一次栅格化都不该发生。"""
    tabs = DetailNotebook(root)
    screen = ScreenView(tabs)
    painted: list[float] = []

    def fake_rasterize(svg: str, scale: float) -> tuple[bytes | None, str]:
        painted.append(scale)
        return None, ""

    screen.rasterize = fake_rasterize  # type: ignore[method-assign]

    screen.show_source(_SVG)
    assert painted == []
    assert screen.svg_source == _SVG
    assert screen.page.get("1.0", "end-1c") == _SVG  # 源码写进了「SVG 源码」页


def test_screen_view_image_format_uses_the_producer(root):
    """`image` 格式的位图由调用方出——只有把终端模型拿在手里的那一侧给得出。"""
    tabs = DetailNotebook(root)
    produced: list[float] = []
    rasterized: list[float] = []

    def fake_produce(scale: float) -> tuple[bytes | None, str]:
        produced.append(scale)
        return None, ""

    screen = ScreenView(tabs, produce=fake_produce)
    screen.rasterize = lambda svg, scale: (rasterized.append(scale), (None, ""))[1]  # type: ignore[method-assign]

    screen.format = FORMAT_IMAGE
    screen.show_screen(_SVG)
    assert produced == [1.0]
    assert rasterized == []  # image 格式不碰 resvg


def test_screen_view_without_a_producer_disables_image(root):
    """跨进程的台子拿不到终端模型：`image` 置灰，位图一律本地栅格化。"""
    tabs = DetailNotebook(root)
    rasterized: list[float] = []
    screen = ScreenView(tabs)
    screen.rasterize = lambda svg, scale: (rasterized.append(scale), (None, ""))[1]  # type: ignore[method-assign]

    assert screen.format == FORMAT_SVG
    states = {
        child.cget("text"): child.state()
        for child in screen.tab.grid_slaves(row=0)[0].winfo_children()
        if isinstance(child, ttk.Radiobutton)
    }
    assert "disabled" in states["image"]
    assert "disabled" not in states["svg"]

    screen.show_screen(_SVG)
    assert rasterized == [1.0]


def test_screen_view_reports_when_the_svg_has_no_size(root):
    """矢量里读不出尺寸就没法铺满画布——留一句提示，别硬画。"""
    tabs = DetailNotebook(root)
    screen = ScreenView(tabs)
    screen.show_screen("<svg/>")
    assert screen.svg_source == "<svg/>"
    assert screen.note  # 有提示


def test_screen_view_reset_clears_the_source(root):
    tabs = DetailNotebook(root)
    screen = ScreenView(tabs)
    screen.show_screen(_SVG)
    screen.reset("未选中会话")
    assert screen.svg_source is None
    assert screen.note == "未选中会话"


def test_detail_notebook_reports_the_visible_page(root):
    tabs = DetailNotebook(root)
    tabs.add_view_page(lambda: None)
    tabs.add_text(Page.RAW, "原始字节", small=True)
    assert tabs.current is Page.VIEW  # 先装的页默认选中
    tabs.show_page(Page.RAW)
    assert tabs.current is Page.RAW


def test_page_change_callback_fires(root):
    """切页要能通知出去——验证台靠它"切到哪页才取哪页的数据"。"""
    tabs = DetailNotebook(root)
    tabs.add_view_page(lambda: None)
    tabs.add_text(Page.RAW, "原始字节", small=True)
    seen: list[int] = []
    tabs.on_page_change(lambda: seen.append(1))

    root.update()  # 装页期间也会排下这个事件，先消化掉
    seen.clear()
    tabs.show_page(Page.RAW)
    root.update()  # <<NotebookTabChanged>> 是异步派发的
    assert seen == [1]


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
