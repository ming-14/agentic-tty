"""openpty 伪终端（Linux）。

对外只有 `OpenPty` 一个类。
"""

from .pty import OpenPty

__all__ = ["OpenPty"]
