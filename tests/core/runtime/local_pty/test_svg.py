"""`localpty` 屏幕渲染测试（纯逻辑）。"""

from __future__ import annotations

import pytest

from agentic_tty.core.runtime.local_pty.screen import PyteScreen
from agentic_tty.core.runtime.local_pty.svg import render_image, render_svg

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def test_svg_geometry_follows_the_cell_grid():
    """8×17 像素一格，与 `pty` 后端同一套。"""
    svg = render_svg(PyteScreen(40, 6))
    assert 'width="320" height="102"' in svg


def test_svg_carries_text_and_styles():
    screen = PyteScreen(20, 2)
    screen.feed(b"\x1b[1;31mBOLD\x1b[0m plain")
    svg = render_svg(screen)
    assert "BOLD" in svg and "plain" in svg
    assert 'fill="#cd0000"' in svg
    assert 'font-weight="bold"' in svg


def test_svg_draws_background_rect():
    screen = PyteScreen(20, 2)
    screen.feed(b"\x1b[44mBG\x1b[0m")
    svg = render_svg(screen)
    assert '<rect x="0" y="0" width="16" height="17" fill="#0000ee"/>' in svg


def test_svg_swaps_colors_on_reverse():
    screen = PyteScreen(20, 2)
    screen.feed(b"\x1b[7mREV\x1b[0m")
    svg = render_svg(screen)
    # 反显：默认前景换到背景位、默认背景换到前景位
    assert 'fill="#0c0c0c"' in svg
    assert 'fill="#e5e5e5"' in svg


def test_svg_escapes_markup():
    screen = PyteScreen(20, 2)
    screen.feed(b"a<b>&c")
    svg = render_svg(screen)
    assert "&lt;" in svg and "&amp;" in svg
    assert "<b>" not in svg


def test_render_image_is_png():
    screen = PyteScreen(20, 2)
    screen.feed(b"hi")
    assert render_image(screen).startswith(_PNG_MAGIC)


def test_render_image_rejects_other_formats():
    """resvg 只吐 PNG，别的格式只能明说做不到。"""
    with pytest.raises(ValueError, match="png"):
        render_image(PyteScreen(20, 2), fmt="jpg")
