"""宿主实现：把程序真的跑起来（PTY / 子进程 / 进程树）。

**唯一**可以 import 原生扩展的包——纯子进程场景因此不拖进 pywezterm。
"""

from __future__ import annotations
