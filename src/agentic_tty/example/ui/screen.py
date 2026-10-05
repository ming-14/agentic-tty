"""「屏幕」页与「SVG 源码」页：格式切换、铺满画布的缩放、栅格化、重渲染缓存。

位图从哪来由调用方决定：`image` 格式是终端模型直接出（把 core 拿在手里的那一侧同步给），
`svg` 格式在这里经 resvg 栅格化——**Tk 的 `PhotoImage` 只吃位图，没有 SVG 解码器**。
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
    raise ImportError("Tk 验证台渲染 SVG 需要 resvg-py（随 agentic-tty 一起装）") from exc

from .common import HINT_COLOR, ask_save, set_text, svg_size
from .notebook import DetailNotebook, Page

FORMAT_IMAGE = "image"
FORMAT_SVG = "svg"
BitmapProducer = Callable[[float], tuple[bytes | None, str] | None]
"""按缩放出 `image` 格式的位图；返回 `None` 表示位图要过一次请求往返，到了再铺画布。"""
# 保存 PNG 用的固定缩放（显示时会按画布大小另算）
EXPORT_SCALE = 2.0


class ScreenView:
    """屏幕页（画布）与 SVG 源码页，外加重渲染缓存。"""

    def __init__(
        self,
        tabs: DetailNotebook,
        *,
        on_format_change: Callable[[], None] | None = None,
        produce: BitmapProducer | None = None,
    ) -> None:
        self._on_format_change = on_format_change
        # `image` 格式的位图从哪来；给不出（跨进程的消费者拿不到终端模型）就没有这个格式
        self._produce = produce
        # 默认 image：满屏 120×40 稳态 13 ms，本地栅格化每帧都要 130 ms（贵 10 倍）。
        # 代价是**第一次**出位图要初始化渲染器（实测 7–10 s），之后就是稳态。
        self._format = tk.StringVar(value=FORMAT_IMAGE if produce is not None else FORMAT_SVG)
        # 矢量 / 画布尺寸 / 格式没变就不重渲染——栅格化不便宜
        self._key: tuple[int, float, str] | None = None
        # 画布上画的是什么；没变就不重建画布项（`reset` 那条路每帧都会走到）
        self._painted: object = None
        self._photo: tk.PhotoImage | None = None
        self._svg_source: str | None = None  # None = 该会话没有屏幕视图
        self._note = ""  # 没有屏幕视图时的提示文字

        self.tab = ttk.Frame(tabs)
        format_row = ttk.Frame(self.tab)
        format_row.grid(row=0, column=0, sticky="w", padx=6, pady=(4, 2))
        ttk.Label(format_row, text="格式").pack(side=tk.LEFT)
        for text, value in (("image", FORMAT_IMAGE), ("svg", FORMAT_SVG)):
            button = ttk.Radiobutton(
                format_row,
                text=text,
                value=value,
                variable=self._format,
                command=self._handle_format_change,
            )
            if value == FORMAT_IMAGE and produce is None:
                button.state(["disabled"])
            button.pack(side=tk.LEFT, padx=(4, 0))
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
        """屏幕位图从哪来：`image`（终端模型直接出）/ `svg`（本地 resvg 栅格化）。"""
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
        """没有屏幕视图时：清掉缓存与源码，两页都只留一句提示。

        未选中会话 / 非终端会话每帧都会走到这里，去重由 `set_text` 与 `set_image` 各自挡住。
        """
        self._key = None
        self._svg_source = None
        self._note = note
        set_text(self.page, note)
        self.set_image(None, note)

    def show_screen(self, svg: str) -> None:
        """「屏幕」页：按当前格式出位图铺满画布。**矢量没变就不重画**。

        `svg` 格式每帧都要本地栅格化（满屏 130 ms），`image` 格式由模型直接出（稳态
        13 ms，第一次要初始化渲染器）——所以拿矢量本身当指纹：它变了画面才可能变。
        先出 SVG，再从**渲染结果自己**读 1.0 倍的像素尺寸：渲染器把尺寸写在输出里，
        不必去别处问"字符格基准是多少"。

        本方法**不碰「SVG 源码」页**：那一页归 `show_source`。
        """
        self._svg_source = svg
        size = svg_size(svg)
        if size is None:
            self._key = None
            self._note = "<渲染结果里没有尺寸，无法铺满画布>"
            self.set_image(None, self._note)
            return
        scale = self.fit_scale(size)
        cache = (hash(svg), round(scale, 4), self.format)
        if cache == self._key:
            return
        self._key = cache
        self._note = ""
        if self.format == FORMAT_IMAGE and self._produce is not None:
            produced = self._produce(scale)
            if produced is not None:  # None = 位图还在路上，到了调用方自己铺
                self.set_image(*produced)
        else:
            self.set_image(*self.rasterize(svg, scale))

    def show_source(self, svg: str) -> None:
        """「SVG 源码」页：只写文本，**不栅格化**。"""
        self._svg_source = svg
        self._note = ""
        set_text(self.page, svg)

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
        """矢量经 resvg 栅格化成位图；出错转成一行提示交给画布。"""
        try:
            return resvg_py.svg_to_bytes(svg_string=svg, zoom=scale), ""
        except Exception as exc:  # SVG 为空
            return None, f"<无屏幕位图: {exc}>"

    def set_image(self, data: bytes | None, note: str) -> None:
        """把位图铺到画布上；没有位图就写提示。**画布内容没变就不重建**。"""
        painted = note if data is None else hash(data)
        if painted == self._painted:
            return
        self._painted = painted
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
        self._key = None  # 格式变了，强制重出位图
        if self._on_format_change is not None:
            self._on_format_change()
