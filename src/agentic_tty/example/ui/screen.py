"""「屏幕」页与「SVG 源码」页：格式切换、铺满画布的缩放、栅格化、重渲染缓存。

位图从哪来由调用方决定：`image` 格式是终端模型直接出（核心层在手，同步给），`svg`
格式在这里经 resvg 栅格化——**Tk 的 PhotoImage 只吃位图，没有 SVG 解码器**。
"""

from __future__ import annotations

import base64
import tkinter as tk
from collections.abc import Callable
from pathlib import Path
from tkinter import messagebox, ttk

try:
    import resvg_py
except ImportError as exc:  # 依赖缺失就说清楚怎么补，不静默降级
    raise ImportError("Tk 验证台渲染 SVG 需要 resvg-py：pip install -e .[gui]") from exc

from .common import HINT_COLOR, ask_save, set_text, svg_size
from .notebook import DetailNotebook, Page

FORMAT_IMAGE = "image"
FORMAT_SVG = "svg"
# 保存 PNG 用的固定缩放（显示时会按画布大小另算）
EXPORT_SCALE = 2.0


class ScreenView:
    """屏幕页（画布 + 格式单选）与 SVG 源码页，外加重渲染缓存。"""

    def __init__(self, tabs: DetailNotebook, *, on_format_change: Callable[[], None]) -> None:
        self._on_format_change = on_format_change
        self._format = tk.StringVar(value=FORMAT_IMAGE)
        # 屏幕 / 格式 / 画布尺寸没变就不重渲染——位图渲染不便宜
        self._key: tuple[object, ...] | None = None
        self._photo: tk.PhotoImage | None = None
        self._svg_source: str | None = None  # None = 该会话没有屏幕视图
        self._note = ""  # 没有屏幕视图时的提示文字

        self.tab = ttk.Frame(tabs)
        format_row = ttk.Frame(self.tab)
        format_row.grid(row=0, column=0, sticky="w", padx=6, pady=(4, 2))
        ttk.Label(format_row, text="格式").pack(side=tk.LEFT)
        for text, value in (("image", FORMAT_IMAGE), ("svg", FORMAT_SVG)):
            ttk.Radiobutton(
                format_row,
                text=text,
                value=value,
                variable=self._format,
                command=self._handle_format_change,
            ).pack(side=tk.LEFT, padx=(4, 0))
        ttk.Label(
            format_row,
            text="（image = 模型直接出位图；svg = 出矢量再栅格化）",
            foreground=HINT_COLOR,
        ).pack(side=tk.LEFT, padx=6)

        # 屏幕铺满画布，不出滚动条
        self.canvas = tk.Canvas(self.tab, highlightthickness=0)
        self.canvas.grid(row=1, column=0, sticky="nsew")
        self.tab.rowconfigure(1, weight=1)
        self.tab.columnconfigure(0, weight=1)

        self.page = tk.Text(tabs, wrap=tk.NONE, font=tabs.mono_small)
        tabs.add_widget(Page.SCREEN, "屏幕", self.tab)
        tabs.add_widget(Page.SVG, "SVG 源码", self.page)

    # ── 状态 ────────────────────────────────────────────────

    @property
    def format(self) -> str:
        return str(self._format.get())

    @format.setter
    def format(self, value: str) -> None:
        """程序化换格式（等价于点那两个单选），顺带作废缓存并通知外层重画。"""
        self._format.set(value)
        self._handle_format_change()

    @property
    def svg_source(self) -> str | None:
        return self._svg_source

    @property
    def note(self) -> str:
        return self._note

    def reset(self, note: str) -> None:
        """没有屏幕视图时：清掉缓存与源码，两页都只留一句提示。"""
        self._key = None
        self._svg_source = None
        self._note = note
        set_text(self.page, note)
        self.set_image(None, note)

    def refresh(
        self,
        *,
        key: tuple[object, ...],
        svg: str,
        produce: Callable[[float], tuple[bytes | None, str]] | None = None,
    ) -> float | None:
        """按 `key`（会话 + 输出偏移）决定要不要重画；返回需要自取位图的缩放。

        先出 SVG，再从**渲染结果自己**读 1.0 倍的像素尺寸——渲染器把尺寸写在输出里，
        不必去别处问"字符格基准是多少"。出 SVG 只要 ~1ms，贵的是栅格化（~25ms），
        所以缓存挡的是栅格化那一步。
        `image` 格式的位图交给 `produce(scale)`；不给就说明位图要走一次请求往返
        （守护进程那一台），此时返回缩放，调用方取回位图后自己 `set_image`。
        """
        size = svg_size(svg)
        if size is None:
            self._key = None
            self._svg_source = svg
            self._note = "<渲染结果里没有尺寸，无法铺满画布>"
            set_text(self.page, svg)
            self.set_image(None, self._note)
            return None
        scale = self.fit_scale(size)
        cache = (*key, self.format, round(scale, 4))
        if cache == self._key:
            return None
        self._key = cache
        self._svg_source = svg
        self._note = ""
        set_text(self.page, svg)
        if self.format == FORMAT_SVG:
            self.set_image(*self.rasterize(svg, scale))
            return None
        if produce is None:
            return scale
        self.set_image(*produce(scale))
        return None

    def fit_scale(self, size: tuple[int, int]) -> float:
        """让屏幕正好铺满画布（不出现滚动条）。画布还没量出尺寸时按 1× 画。"""
        base_width, base_height = size
        if base_width <= 0 or base_height <= 0:
            return 1.0
        width, height = self.canvas.winfo_width(), self.canvas.winfo_height()
        if width <= 1 or height <= 1:
            return 1.0
        return min(width / base_width, height / base_height)

    @staticmethod
    def rasterize(svg: str, scale: float) -> tuple[bytes | None, str]:
        """`svg` 格式：矢量经 resvg 栅格化成位图。"""
        try:
            return resvg_py.svg_to_bytes(svg_string=svg, zoom=scale), ""
        except Exception as exc:  # SVG 为空
            return None, f"<无屏幕位图: {exc}>"

    def set_image(self, data: bytes | None, note: str) -> None:
        self.canvas.delete("all")
        self._photo = None  # 必须留引用，否则 Tk 会把图回收掉
        if data is None:
            self.canvas.create_text(12, 12, anchor="nw", text=note, fill=HINT_COLOR)
            return
        # Tk 的 PhotoImage 只吃位图（8.6 起原生支持 PNG）；create_image 默认居中
        try:
            self._photo = tk.PhotoImage(data=base64.b64encode(data).decode("ascii"))
        except tk.TclError as exc:  # 位图坏了不该把界面搞崩
            self.canvas.create_text(12, 12, anchor="nw", text=f"<位图无法显示: {exc}>")
            return
        self.canvas.create_image(
            self.canvas.winfo_width() / 2,
            self.canvas.winfo_height() / 2,
            image=self._photo,
        )

    def save_svg(self) -> str | None:
        """把当前 SVG 源码存盘；没源码或用户取消返回 None。"""
        if self._svg_source is None:
            messagebox.showinfo("保存 SVG", self._note or "没有 SVG 可保存")
            return None
        path = ask_save(
            title="保存屏幕 SVG",
            initial="screen.svg",
            extension=".svg",
            filetype=("SVG", "*.svg"),
        )
        if not path:
            return None
        Path(path).write_text(self._svg_source, encoding="utf-8")
        return path

    def _handle_format_change(self) -> None:
        self._key = None  # 格式变了，强制重渲染
        self._on_format_change()
