"""`localpty` 的终端模型适配测试（纯逻辑，不需要真 PTY）。"""

from __future__ import annotations

from agentic_tty.core.runtime.local_pty.screen import PyteScreen
from agentic_tty.core.runtime.local_pty.svg import render_svg


def test_feed_returns_application_responses():
    """DSR / DA 之类的应答必须回吐给应用——不回写它会一直等下去。"""
    screen = PyteScreen(20, 4)
    screen.feed(b"hi")
    assert screen.feed(b"\x1b[6n") == b"\x1b[1;3R"
    assert screen.feed(b"\x1b[c") != b""


def test_text_drops_trailing_blanks():
    screen = PyteScreen(20, 4)
    screen.feed(b"one\r\ntwo")
    assert screen.text() == "one\ntwo"


def test_cells_are_ragged_with_wide_char_stubs():
    """端口要求：宽字符占两格、续格为空串；行不铺满整宽。"""
    screen = PyteScreen(10, 3)
    screen.feed("你好ab".encode())
    assert screen.cells() == (("你", "", "好", "", "a", "b"), (), ())


def test_alt_screen_restores_the_main_screen():
    """pyte 本身不实现交替屏，这一层是补出来的。"""
    screen = PyteScreen(20, 4)
    screen.feed(b"main")
    screen.feed(b"\x1b[?1049h\x1b[2J\x1b[HTUI")
    assert screen.text() == "TUI"
    screen.feed(b"\x1b[?1049l")
    assert screen.text() == "main"


def test_alt_screen_does_not_pollute_history():
    """全屏程序每帧整屏重画，让它往 scrollback 里塞只会污染历史。"""
    screen = PyteScreen(20, 2, history=50)
    screen.feed(b"\x1b[?1049h")
    for index in range(10):
        screen.feed(f"frame-{index}\r\n".encode())
    screen.feed(b"\x1b[?1049l")
    assert "frame-" not in screen.full_text()


def test_title_comes_from_osc():
    screen = PyteScreen(20, 4)
    screen.feed(b"\x1b]0;my-title\x07")
    assert screen.title() == "my-title"


def test_rebuild_bytes_round_trips_through_a_fresh_model():
    """重建字节喂进一个空模型，屏幕与样式都该还原。"""
    screen = PyteScreen(20, 4)
    screen.feed(b"\x1b[1;31mred\x1b[0m plain\r\nsecond")
    rebuild = screen.rebuild_bytes()
    assert rebuild.startswith(b"\x1bc")

    fresh = PyteScreen(20, 4)
    fresh.feed(rebuild)
    assert fresh.text() == screen.text()
    assert fresh.cells() == screen.cells()
    assert render_svg(fresh) == render_svg(screen)


def test_rebuild_bytes_carries_the_cursor():
    screen = PyteScreen(20, 4)
    screen.feed(b"abc")
    fresh = PyteScreen(20, 4)
    fresh.feed(screen.rebuild_bytes())
    assert fresh.cursor() == screen.cursor()


def test_rebuild_bytes_keeps_scrollback():
    screen = PyteScreen(20, 2, history=50)
    screen.feed(b"a\r\nb\r\nc\r\nd")
    assert "a" in screen.full_text()
    fresh = PyteScreen(20, 2, history=50)
    fresh.feed(screen.rebuild_bytes())
    assert fresh.full_text() == screen.full_text()


def test_resize_changes_the_grid():
    screen = PyteScreen(20, 4)
    screen.feed(b"x" * 30)
    screen.resize(40, 6)
    assert (screen.columns, screen.lines) == (40, 6)
    assert len(screen.cells()) == 6
