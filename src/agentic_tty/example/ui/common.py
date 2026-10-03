"""纯 Tk 的小工具：等宽字体、提示色、整页文本、SVG 尺寸、另存为对话框。

这一格不认识 core——只吃字符串与字节，回调都由验证台注入。
"""

from __future__ import annotations

import re
import tkinter as tk
from tkinter import filedialog
from tkinter import font as tkfont


def fixed_font(size: int) -> tkfont.Font:
    """平台默认等宽字体的副本——不硬编码 Consolas 这类平台专有字体（Linux 上没有）。"""
    font = tkfont.nametofont("TkFixedFont").copy()
    font.configure(size=size)
    return font


# 提示语 / 占位文字的灰字色
HINT_COLOR = "#888780"


def set_text(widget: tk.Text, text: str) -> None:
    """整页替换文本：内容没变就不动（省一次重排），原本贴底就保持贴底。"""
    if widget.get("1.0", "end-1c") == text:
        return
    at_bottom = widget.yview()[1] >= 0.999
    widget.delete("1.0", tk.END)
    widget.insert("1.0", text)
    if at_bottom:
        widget.see(tk.END)


def svg_size(svg: str) -> tuple[int, int] | None:
    """渲染结果自带的像素尺寸（1.0 倍）——根元素上写着 width / height。"""
    root = svg.split(">", 1)[0]
    width = re.search(r'\bwidth="([\d.]+)"', root)
    height = re.search(r'\bheight="([\d.]+)"', root)
    if width is None or height is None:
        return None
    return int(float(width.group(1))), int(float(height.group(1)))


def ask_save(
    *, title: str, initial: str, extension: str, filetype: tuple[str, str]
) -> str:
    """另存为对话框；用户取消返回空串（写文件的活留给调用方）。"""
    return filedialog.asksaveasfilename(
        title=title,
        defaultextension=extension,
        initialfile=initial,
        filetypes=[filetype],
    )
