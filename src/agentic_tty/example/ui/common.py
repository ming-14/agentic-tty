"""共享小工具：字体与颜色、整页文本、SVG 尺寸、另存为 / 选目录对话框、命令行拆分。

这一格不认识 core——只吃字符串与字节，回调都由验证台注入。
"""

from __future__ import annotations

import re
import shlex
import sys
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


def split_command(text: str) -> tuple[str, ...]:
    """命令框里的一行 → argv，按**本平台自己的规则**拆；拆不出东西返回空元组。

    POSIX 的 `shlex` 把 `\\` 当转义符，Windows 路径 `C:\\Windows\\System32\\cmd.exe`
    会被啃成 `C:WindowsSystem32cmd.exe`，进程根本起不来。Windows 上照 `CreateProcessW`
    拆命令行用的那套规则走——两边各用各的，而不是拿一套规则套所有平台。
    """
    if not text.strip():
        return ()
    if sys.platform == "win32":
        return _split_windows(text)
    return tuple(shlex.split(text))


def _split_windows(text: str) -> tuple[str, ...]:
    """`CommandLineToArgvW`：与 `CreateProcessW` 拆命令行用的是同一套规则。"""
    import ctypes
    from ctypes import wintypes

    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    shell32.CommandLineToArgvW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    shell32.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL

    count = ctypes.c_int(0)
    argv = shell32.CommandLineToArgvW(text, ctypes.byref(count))
    if not argv:
        raise OSError(ctypes.get_last_error(), f"CommandLineToArgvW 失败: {text!r}")
    try:
        return tuple(argv[index] for index in range(count.value))
    finally:
        kernel32.LocalFree(argv)


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


def ask_directory(*, title: str, initial: str = "") -> str:
    """选目录对话框；用户取消返回空串（只让选已存在的目录）。"""
    return filedialog.askdirectory(title=title, initialdir=initial or None, mustexist=True)
