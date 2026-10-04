"""可见屏幕的 SVG 与位图。

格式逐字对齐 pywezterm 的 `render_svg`：8×17 像素一格、同一套字体与调色板——这样
`example/` 那套解析与 Tk 台子不必为两个后端分家。
"""

from __future__ import annotations

from collections.abc import Iterator

from ..errors import DependencyMissing

CELL_WIDTH = 8
CELL_HEIGHT = 17

_BACKGROUND = "#0c0c0c"
_FOREGROUND = "#e5e5e5"

_PALETTE = (
    "#000000", "#cd0000", "#00cd00", "#cdcd00",
    "#0000ee", "#cd00cd", "#00cdcd", "#e5e5e5",
    "#7f7f7f", "#ff0000", "#00ff00", "#ffff00",
    "#5c5cff", "#ff00ff", "#00ffff", "#ffffff",
)
"""Windows 控制台调色板——pywezterm 用的就是这套。"""

_ANSI_ORDER = ("black", "red", "green", "brown", "blue", "magenta", "cyan", "white")
"""pyte 的 16 色名，按 SGR 的编号顺序（`brown` 就是通常说的 yellow）。"""

_FONT = (
    'text{font-family:Consolas,"Microsoft YaHei",monospace;font-size:15px;'
    "dominant-baseline:text-before-edge;white-space:pre}"
)


def _fill(color: str, *, background: bool) -> str:
    """pyte 的颜色表示 → 十六进制取值。"""
    if color == "default":
        return _BACKGROUND if background else _FOREGROUND
    if len(color) == 6:
        return f"#{color}"
    bright = color.startswith("bright")
    index = _ANSI_ORDER.index(color[len("bright") :] if bright else color)
    return _PALETTE[index + (8 if bright else 0)]


def _style(char) -> tuple[str, str, bool]:
    """格 → (前景, 背景, 加粗)。反显在这里换位，这样连默认色对默认色也换得对。"""
    foreground = _fill(char.fg, background=False)
    background = _fill(char.bg, background=True)
    if char.reverse:
        foreground, background = background, foreground
    return foreground, background, char.bold


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _runs(row, columns: int) -> Iterator[tuple[int, int, str, str, bool]]:
    """把一行按「前景 / 背景 / 加粗」切成 run——pywezterm 也只按这三样切。"""
    start = 0
    key: tuple[str, str, bool] | None = None
    for x in range(columns):
        current = _style(row[x])
        if key is None:
            key, start = current, x
        elif current != key:
            yield start, x, *key
            key, start = current, x
    if key is not None:
        yield start, columns, *key


def _row_markup(row, y: int, columns: int) -> Iterator[str]:
    """一行：先铺非默认背景的矩形，再按 run 画文字。"""
    runs = list(_runs(row, columns))
    top = y * CELL_HEIGHT
    for start, end, _fg, bg, _bold in runs:
        if bg == _BACKGROUND:
            continue
        yield (
            f'<rect x="{start * CELL_WIDTH}" y="{top}"'
            f' width="{(end - start) * CELL_WIDTH}" height="{CELL_HEIGHT}" fill="{bg}"/>'
        )
    for start, end, fg, _bg, bold in runs:
        text = _escape("".join(row[x].data for x in range(start, end)))
        if not text.strip():
            continue
        weight = ' font-weight="bold"' if bold else ""
        yield (
            f'<text x="{start * CELL_WIDTH}" y="{top}"'
            f' fill="{fg}"{weight}>{text}</text>'
        )


def render_svg(screen) -> str:
    """把可见屏幕画成 SVG。`screen` 是 `PyteScreen`。"""
    columns, lines = screen.columns, screen.lines
    width, height = columns * CELL_WIDTH, lines * CELL_HEIGHT
    parts = [
        '<svg xmlns="http://www.w3.org/2000/svg" xml:space="preserve"'
        f' width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        f'<rect width="100%" height="100%" fill="{_BACKGROUND}"/>',
        f"<style>{_FONT}</style>",
    ]
    for y in range(lines):
        parts.extend(_row_markup(screen.row(y), y, columns))
    parts.append("</svg>")
    return "".join(parts)


def render_image(screen, *, scale: float = 1.0, fmt: str = "png") -> bytes:
    """把可见屏幕光栅化成位图。

    `resvg` 只吐 PNG，所以这里也只有 PNG——要别的格式得自己再编一遍码。
    """
    if fmt != "png":
        raise ValueError(f"localpty 的位图只有 png，收到 {fmt!r}")
    try:
        import resvg_py
    except ImportError as exc:
        raise DependencyMissing("位图渲染需要 resvg-py（随 agentic-tty 一起装）") from exc
    return resvg_py.svg_to_bytes(svg_string=render_svg(screen), zoom=scale)
