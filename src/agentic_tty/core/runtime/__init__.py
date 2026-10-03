"""宿主实现与运行时驱动：把程序真的跑起来（PTY / 子进程 / 进程树），并驱动它们
（读 / 写线程、输入队列、会话运行时）。

**唯一**可以 import 原生扩展的包——纯子进程场景因此不拖进 pywezterm。
"""

from __future__ import annotations
