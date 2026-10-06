"""示例层的共享 Tk 控件：验证台长得一样的那部分。

**这一格不认识 core**——只吃字符串与字节，会话数据由验证台渲染好再喂进来
（`core_test_console/render.py`）。所以换任何一台台子都能直接用。
"""

from .bars import DirBar, InputBar, SessionBar, SizeBar, StatusBar
from .common import HINT_COLOR, ask_directory, ask_save, set_text, svg_size
from .notebook import DetailNotebook, Page, ViewRange
from .screen import EXPORT_SCALE, FORMAT_IMAGE, FORMAT_SVG, ScreenView
from .tree import SessionTree

__all__ = [
    "EXPORT_SCALE",
    "FORMAT_IMAGE",
    "FORMAT_SVG",
    "HINT_COLOR",
    "DetailNotebook",
    "DirBar",
    "InputBar",
    "Page",
    "ScreenView",
    "SessionBar",
    "SessionTree",
    "SizeBar",
    "StatusBar",
    "ViewRange",
    "ask_directory",
    "ask_save",
    "set_text",
    "svg_size",
]
