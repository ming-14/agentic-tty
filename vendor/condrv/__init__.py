"""ConDrv 直连伪终端（Windows）。

对外只有 `ConDrvPty` 一个类；`win32` 是它自用的声明层。
"""

from .pty import ConDrvPty, find_conhost

__all__ = ["ConDrvPty", "find_conhost"]
