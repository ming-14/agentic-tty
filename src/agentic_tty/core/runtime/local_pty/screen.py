"""pyte 终端模型适配。

pyte 缺两样端口要的东西，这里补齐：

- **交替屏**：pyte 只把 `?1049h` 记进 `mode`，不切缓冲（源码里 `1049` / `1047` /
  `47` 一次都没出现）。不补的话 vim / less / top 退出时会把最后一帧留在屏幕上。
- **重建字节**：pyte 不回吐 ANSI，得从它的格栅自己拼。

`pywezterm` 那边是模型自带这两样，所以这个后端**不碰 pywezterm**。
"""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Iterable

import pyte
from pyte.screens import Cursor, History, StaticDefaultDict

#: 滚动历史保留行数，与 `pty` 后端的默认 scrollback 对齐。
_HISTORY_LINES = 10000

_ALT_SCREEN_MODES = frozenset({47, 1047, 1049})
"""交替屏的三种写法。1049 额外带光标保存/恢复，这里统一按「存起主屏与光标、换一块
空缓冲」处理——差别只在进入时光标落在哪，而全屏程序进来后都会自己定位。"""

_EMPTY_HISTORY = History(deque(maxlen=0), deque(maxlen=0), 0.5, 0, 0)
"""交替屏期间用的空历史。全屏程序每帧整屏重画，让它往 scrollback 里塞只会污染历史。"""

_ANSI_ORDER = ("black", "red", "green", "brown", "blue", "magenta", "cyan", "white")
"""pyte 的 16 色名，按 SGR 的编号顺序（`brown` 就是通常说的 yellow）。"""


def _is_private(mode: int) -> bool:
    """pyte 把私有模式左移 5 位存进同一个集合（见 `Screen.set_mode`），这里反推。"""
    return mode >= 32 and mode % 32 == 0


def _color_code(color: str, *, background: bool) -> str:
    """pyte 的颜色表示 → SGR 参数。

    pyte 给三种形态：`'default'`、16 色名（`'red'` / `'brightred'`）、6 位十六进制
    （256 色与真彩色都化成这个）。
    """
    if color == "default":
        return "49" if background else "39"
    if len(color) == 6:
        red, green, blue = (int(color[i : i + 2], 16) for i in (0, 2, 4))
        return f"{48 if background else 38};2;{red};{green};{blue}"
    bright = color.startswith("bright")
    index = _ANSI_ORDER.index(color[len("bright") :] if bright else color)
    return str(index + (8 if bright else 0) + (40 if background else 30))


def _sgr(char) -> str:
    """一个格的样式 → SGR 参数串；空串表示全默认。"""
    codes: list[str] = []
    if char.bold:
        codes.append("1")
    if char.italics:
        codes.append("3")
    if char.underscore:
        codes.append("4")
    if char.reverse:
        codes.append("7")
    if char.strikethrough:
        codes.append("9")
    if char.fg != "default":
        codes.append(_color_code(char.fg, background=False))
    if char.bg != "default":
        codes.append(_color_code(char.bg, background=True))
    return ";".join(codes)


def _render_row(row, columns: int) -> str:
    """一行 → SGR + 文本；行尾空白不发（可见区每行有 `\\x1b[K` 兜底）。"""
    last = max((x for x in range(columns) if row[x].data.strip()), default=-1)
    if last < 0:
        return ""
    parts: list[str] = []
    current = ""
    for x in range(last + 1):
        char = row[x]
        sgr = _sgr(char)
        if sgr != current:
            parts.append(f"\x1b[{sgr}m" if sgr else "\x1b[0m")
            current = sgr
        parts.append(char.data)
    if current:
        parts.append("\x1b[0m")
    return "".join(parts)


def _row_text(row, columns: int) -> str:
    """一行 → 文本（跳过宽字符的续格）。"""
    return "".join(row[x].data for x in range(columns) if row[x].data).rstrip()


def _row_cells(row, columns: int) -> tuple[str, ...]:
    """一行 → 字符格栅。行尾空白截掉——宽字符的续格是空串，也算空白。"""
    cells = [row[x].data for x in range(columns)]
    while cells and not cells[-1].strip():
        cells.pop()
    return tuple(cells)


def _join(lines: Iterable[str]) -> str:
    """行尾空白与尾部空行都去掉——对齐 `pywezterm.Terminal.text()`。"""
    trimmed = [line.rstrip() for line in lines]
    while trimmed and not trimmed[-1]:
        trimmed.pop()
    return "\n".join(trimmed)


class _TerminalScreen(pyte.HistoryScreen):
    """带交替屏与应答收集的 `HistoryScreen`。"""

    def __init__(self, columns: int, lines: int, history: int) -> None:
        super().__init__(columns, lines, history=history)
        self._responses: list[str] = []
        self._main: tuple | None = None

    # 应答（DSR / DA 等）：pyte 把它们交给 `write_process_input`，默认是空实现
    def write_process_input(self, data: str) -> None:
        self._responses.append(data)

    def drain_responses(self) -> bytes:
        if not self._responses:
            return b""
        text = "".join(self._responses)
        self._responses.clear()
        return text.encode()

    def set_mode(self, *modes: int, **kwargs) -> None:
        if kwargs.get("private") and _ALT_SCREEN_MODES.intersection(modes):
            self._enter_alt()
        super().set_mode(*modes, **kwargs)

    def reset_mode(self, *modes: int, **kwargs) -> None:
        if kwargs.get("private") and _ALT_SCREEN_MODES.intersection(modes):
            self._exit_alt()
        super().reset_mode(*modes, **kwargs)

    def _enter_alt(self) -> None:
        if self._main is not None:
            return
        self._main = (self.buffer, self.cursor, self.history)
        self.buffer = defaultdict(lambda: StaticDefaultDict(self.default_char))
        self.history = _EMPTY_HISTORY
        self.dirty.update(range(self.lines))

    def _exit_alt(self) -> None:
        if self._main is None:
            return
        self.buffer, self.cursor, self.history = self._main
        self._main = None
        self.dirty.update(range(self.lines))


class PyteScreen:
    """端口的终端模型侧：喂字节拿应答、读屏幕、拼重建字节。"""

    def __init__(self, cols: int, rows: int, *, history: int = _HISTORY_LINES) -> None:
        self._screen = _TerminalScreen(cols, rows, history)
        self._stream = pyte.ByteStream(self._screen)

    @property
    def columns(self) -> int:
        return self._screen.columns

    @property
    def lines(self) -> int:
        return self._screen.lines

    def feed(self, data: bytes) -> bytes:
        """喂一段字节，返回模型要回写给应用的应答。"""
        self._stream.feed(data)
        return self._screen.drain_responses()

    def resize(self, cols: int, rows: int) -> None:
        self._screen.resize(rows, cols)  # pyte 的参数序是 (lines, columns)

    def row(self, y: int) -> list:
        """可见区第 y 行的格（长度 = 列数）；渲染层读样式用。"""
        line = self._screen.buffer[y]
        return [line[x] for x in range(self._screen.columns)]

    def cursor(self) -> tuple[int, int, bool]:
        cursor: Cursor = self._screen.cursor
        return cursor.y, cursor.x, cursor.hidden

    def title(self) -> str:
        return self._screen.title

    def text(self) -> str:
        """可见屏幕纯文本。"""
        return _join(self._screen.display)

    def full_text(self) -> str:
        """含滚动历史的可见文本。"""
        columns = self._screen.columns
        history = [_row_text(line, columns) for line in self._screen.history.top]
        return _join(history + self._screen.display)

    def cells(self) -> tuple[tuple[str, ...], ...]:
        """可见屏幕字符格栅（ragged：行不铺满整宽，空行是空元组）。"""
        columns = self._screen.columns
        return tuple(
            _row_cells(self._screen.buffer[y], columns) for y in range(self._screen.lines)
        )

    def rebuild_bytes(self) -> bytes:
        """重建字节：RIS + 模式恢复 + 滚动历史 + 可见区。

        与 `pty` 后端的四段式对齐——喂进一个空终端模型即可还原当前状态。**不是屏幕
        内容**：要屏幕内容用 `text()` / `full_text()`。
        """
        screen = self._screen
        columns = screen.columns
        parts = [b"\x1bc", self._mode_restore()]
        history = [_render_row(line, columns) for line in screen.history.top]
        if history:
            parts.append(("\r\n".join(history) + "\r\n").encode())
            # 把还压在可见区的那几行也推进历史。每写一行带一次 LF，屏上总会剩下最后
            # `lines - 1` 行；不推走，它们就会被下面按绝对位置写的可见区盖掉。
            parts.append(b"\r\n" * (screen.lines - 1))
        for y in range(screen.lines):
            parts.append(f"\x1b[{y + 1};1H\x1b[K".encode())
            parts.append(_render_row(screen.buffer[y], columns).encode())
        row, column, hidden = self.cursor()
        parts.append(f"\x1b[{row + 1};{column + 1}H".encode())
        parts.append(b"\x1b[?25l" if hidden else b"\x1b[?25h")
        return b"".join(parts)

    def _mode_restore(self) -> bytes:
        """把当前模式反推成 DECSET 序列。"""
        private: list[str] = []
        plain: list[str] = []
        for mode in sorted(self._screen.mode):
            if _is_private(mode):
                private.append(f"\x1b[?{mode >> 5}h")
            else:
                plain.append(f"\x1b[{mode}h")
        return "".join(private + plain).encode()
