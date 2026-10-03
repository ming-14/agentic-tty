"""右侧详情页容器：页签的装入、整页文本写入、按会话形态开关页签。"""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from enum import StrEnum
from tkinter import ttk

from .common import HINT_COLOR, fixed_font, set_text


class Page(StrEnum):
    """详情页的键（两台验证台共用同一套页名，各台按需要装自己那几页）。"""

    SCREEN = "screen"
    SVG = "svg"
    VIEW = "view"
    CELLS = "cells"
    RAW = "raw"
    SUB = "sub"
    EVENTS = "events"
    PROCS = "procs"


class ViewRange(StrEnum):
    """「视图」页的范围：core 的两项返回数据，语义不同。"""

    SCREEN = "screen"
    FULL = "full"


class DetailNotebook(ttk.Notebook):
    """文本页的增删与写入。

    「屏幕」「SVG 源码」两页由 `ScreenView` 自己 `add_widget` 进来——那两页有渲染状态，
    归它管；这里只负责装下它们，以及给其余页提供 `set_text` / `page_state` 这一套入口。
    """

    def __init__(self, parent: tk.Misc) -> None:
        super().__init__(parent)
        self.mono = fixed_font(10)
        self.mono_small = fixed_font(9)
        self.view_mode = tk.StringVar(value=ViewRange.SCREEN)
        self._widgets: dict[Page, tk.Widget] = {}
        self._texts: dict[Page, tk.Text] = {}

    def add_widget(self, key: Page, label: str, widget: tk.Widget) -> None:
        self._widgets[key] = widget
        self.add(widget, text=label)

    def add_text(self, key: Page, label: str, *, small: bool = False) -> None:
        text = tk.Text(self, wrap=tk.NONE, font=self.mono_small if small else self.mono)
        self._texts[key] = text
        self.add_widget(key, label, text)

    def add_view_page(self, on_change: Callable[[], None] | None = None) -> None:
        """「视图」页；传 `on_change` 才带「可见屏幕 / 全量输出」单选行。

        守护进程那台的范围由服务端给，页面上不放单选，所以 `on_change` 可以不传。
        """
        frame = ttk.Frame(self)
        if on_change is not None:
            row = ttk.Frame(frame)
            row.pack(fill=tk.X, padx=6, pady=(4, 0))
            ttk.Label(row, text="范围").pack(side=tk.LEFT)
            for text, value in (
                ("可见屏幕", ViewRange.SCREEN),
                ("全量输出", ViewRange.FULL),
            ):
                ttk.Radiobutton(
                    row, text=text, value=value, variable=self.view_mode, command=on_change
                ).pack(side=tk.LEFT, padx=(4, 0))
            ttk.Label(
                row, text="（全量输出 = 含滚动历史的可见文本）", foreground=HINT_COLOR
            ).pack(side=tk.LEFT, padx=6)
        text = tk.Text(frame, wrap=tk.NONE, font=self.mono)
        text.pack(fill=tk.BOTH, expand=True)
        self._texts[Page.VIEW] = text
        self.add_widget(Page.VIEW, "视图", frame)

    def text(self, key: Page) -> tk.Text:
        return self._texts[key]

    def set_text(self, key: Page, text: str) -> None:
        set_text(self._texts[key], text)

    def page_state(self, key: Page) -> str:
        """页签状态（`normal` / `hidden`）。"""
        return str(self.tab(self._widgets[key], "state"))

    def set_page_state(self, key: Page, state: str) -> None:
        self.tab(self._widgets[key], state=state)

    def show_page(self, key: Page) -> None:
        self.select(self._widgets[key])
