"""示例层的共享 Tk 控件：两台验证台长得一样的那部分。

**这一格不认识 core**——只吃字符串与字节，会话数据由验证台渲染好再喂进来（core 那台在
`core_test/render.py`，守护进程那台由服务端给）。所以换任何一台台子都能直接用。
"""

from __future__ import annotations

from .bars import InputBar, SessionBar, SizeBar, StatusBar
from .common import HINT_COLOR, ask_save, set_text, svg_size
from .notebook import DetailNotebook, Page, ViewRange
from .screen import EXPORT_SCALE, ScreenView
from .tree import SessionTree

__all__ = [
    "EXPORT_SCALE",
    "HINT_COLOR",
    "DetailNotebook",
    "InputBar",
    "Page",
    "ScreenView",
    "SessionBar",
    "SessionTree",
    "SizeBar",
    "StatusBar",
    "ViewRange",
    "ask_save",
    "set_text",
    "svg_size",
]
