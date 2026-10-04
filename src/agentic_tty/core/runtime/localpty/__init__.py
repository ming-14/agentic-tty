"""`localpty` 宿主：平台 PTY 原语 + pyte 终端模型。"""

from .host import LocalPtyHost, require_primitive

__all__ = ["LocalPtyHost", "require_primitive"]
