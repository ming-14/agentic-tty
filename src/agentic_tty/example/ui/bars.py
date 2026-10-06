"""五条横向控件：顶栏（模式 + 命令 + 动作）、工作目录行、输入行、尺寸行、状态栏。"""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable, Sequence
from tkinter import ttk

from .common import HINT_COLOR, ask_directory


class SessionBar(ttk.Frame):
    """顶栏：模式单选 + 命令框 + 一排动作按钮 + 提示语。

    `command` 直接给原始控件而不是读写封装：命令框要在「下拉选假程序」与「自由输入真
    命令」两种形态之间来回改，形态由 `command_values` 决定（不给就是纯输入框）。
    """

    def __init__(
        self,
        parent: tk.Misc,
        *,
        modes: Sequence[tuple[str, str]],
        value: str,
        buttons: Sequence[tuple[str, Callable[[], None]]],
        hint: str = "",
        command_values: Sequence[str] | None = None,
        on_mode_change: Callable[[], None] | None = None,
        padding: tuple[int, int] = (8, 6),
    ) -> None:
        super().__init__(parent, padding=padding)
        ttk.Label(self, text="模式").pack(side=tk.LEFT)
        self.mode = tk.StringVar(value=value)
        for text, mode in modes:
            ttk.Radiobutton(
                self, text=text, value=mode, variable=self.mode, command=on_mode_change
            ).pack(side=tk.LEFT, padx=(4, 0))
        ttk.Label(self, text="   命令").pack(side=tk.LEFT)
        # 给了下拉项就是可选可输的 Combobox（fake 的假程序），没给就是纯输入框
        self.command: ttk.Entry | ttk.Combobox = (
            ttk.Combobox(self, values=list(command_values), width=18)
            if command_values is not None
            else ttk.Entry(self, width=32)
        )
        self.command.pack(side=tk.LEFT)
        for text, command in buttons:
            ttk.Button(self, text=text, command=command).pack(side=tk.LEFT, padx=(6, 0))
        if hint:
            ttk.Label(self, text=hint, foreground=HINT_COLOR).pack(side=tk.LEFT, padx=6)
        self.pack(fill=tk.X)


class DirBar(ttk.Frame):
    """工作目录行：路径框 + 浏览。**留空 = 用默认目录**（谁起会话就由谁的当前目录说了算）。

    路径原样往下传：相对路径的语义由起会话的那一侧解释，这里不替它猜。
    """

    def __init__(
        self,
        parent: tk.Misc,
        *,
        hint: str = "",
        padding: tuple[int, int] = (8, 0),
    ) -> None:
        super().__init__(parent, padding=padding)
        ttk.Label(self, text="工作目录").pack(side=tk.LEFT)
        self.path = ttk.Entry(self)
        self.path.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(self, text="浏览…", command=self._browse).pack(side=tk.LEFT, padx=(4, 0))
        if hint:
            ttk.Label(self, text=hint, foreground=HINT_COLOR).pack(side=tk.LEFT, padx=6)
        self.pack(fill=tk.X)

    def _browse(self) -> None:
        """选目录；取消（空串）就保持原样。"""
        chosen = ask_directory(title="选择工作目录", initial=self.directory or "")
        if chosen:
            self.set_directory(chosen)

    @property
    def directory(self) -> str | None:
        """填了就是它（去掉首尾空白）；留空 = `None`，交给默认目录。"""
        return self.path.get().strip() or None

    def set_directory(self, value: str | None) -> None:
        self.path.delete(0, tk.END)
        self.path.insert(0, value or "")


class InputBar(ttk.Frame):
    """输入行：文本框 + 发送 / 发送+换行 / Ctrl+C，可再挂「关 stdin」这类会话动作。"""

    def __init__(
        self,
        parent: tk.Misc,
        *,
        on_send: Callable[[bool], None],
        on_interrupt: Callable[[], None],
    ) -> None:
        super().__init__(parent)
        self.entry = ttk.Entry(self)
        self.entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.entry.bind("<Return>", lambda _event: on_send(True))
        ttk.Button(self, text="发送", command=lambda: on_send(False)).pack(
            side=tk.LEFT, padx=4
        )
        ttk.Button(self, text="发送+换行", command=lambda: on_send(True)).pack(
            side=tk.LEFT
        )
        # 控制字符编码属于命令层：验证台（命令层占位）自己做，核心层只收字节
        ttk.Button(self, text="Ctrl+C", command=on_interrupt).pack(
            side=tk.LEFT, padx=(4, 0)
        )
        self.pack(fill=tk.X, pady=(6, 0))

    @property
    def text(self) -> str:
        return self.entry.get()

    @text.setter
    def text(self, value: str) -> None:
        self.clear()
        self.entry.insert(0, value)

    def clear(self) -> None:
        self.entry.delete(0, tk.END)

    def add_button(self, text: str, command: Callable[[], None]) -> ttk.Button:
        button = ttk.Button(self, text=text, command=command)
        button.pack(side=tk.LEFT, padx=(4, 0))
        return button


class SizeBar(ttk.Frame):
    """尺寸行：宽高 + 应用，右侧跟导出按钮（都是 pty 专属，一起开关）。"""

    def __init__(
        self,
        parent: tk.Misc,
        *,
        on_apply: Callable[[], None],
        on_save_svg: Callable[[], None],
        on_save_png: Callable[[], None],
    ) -> None:
        super().__init__(parent)
        ttk.Label(self, text="尺寸").pack(side=tk.LEFT)
        self.cols = ttk.Entry(self, width=5)
        self.cols.insert(0, "80")
        self.cols.pack(side=tk.LEFT)
        ttk.Label(self, text="×").pack(side=tk.LEFT)
        self.rows = ttk.Entry(self, width=5)
        self.rows.insert(0, "24")
        self.rows.pack(side=tk.LEFT)
        self.apply_btn = ttk.Button(self, text="应用", command=on_apply)
        self.apply_btn.pack(side=tk.LEFT, padx=4)
        self.save_svg_btn = ttk.Button(self, text="保存 SVG", command=on_save_svg)
        self.save_svg_btn.pack(side=tk.LEFT, padx=(12, 0))
        self.save_png_btn = ttk.Button(self, text="保存 PNG", command=on_save_png)
        self.save_png_btn.pack(side=tk.LEFT, padx=4)
        self.pack(fill=tk.X, pady=(4, 0))

    @property
    def size(self) -> tuple[int, int]:
        """宽高；不是整数会抛 `ValueError`，弹不弹框由调用方定。"""
        return int(self.cols.get()), int(self.rows.get())

    def set_size(self, cols: int, rows: int) -> None:
        for entry, value in ((self.cols, cols), (self.rows, rows)):
            entry.delete(0, tk.END)
            entry.insert(0, str(value))

    def set_enabled(self, enabled: bool) -> None:
        state = ["!disabled"] if enabled else ["disabled"]
        for widget in (self.apply_btn, self.save_svg_btn, self.save_png_btn, self.cols, self.rows):
            widget.state(state)


class StatusBar(ttk.Frame):
    """底部状态栏。"""

    def __init__(self, parent: tk.Misc) -> None:
        super().__init__(parent)
        self._text = tk.StringVar(value="就绪")
        ttk.Label(self, textvariable=self._text, relief=tk.SUNKEN, anchor=tk.W).pack(
            fill=tk.X
        )
        self.pack(fill=tk.X)

    @property
    def text(self) -> str:
        return self._text.get()

    def set(self, text: str) -> None:
        self._text.set(text)
