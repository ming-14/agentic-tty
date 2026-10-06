"""`localpty` 宿主：平台 PTY 原语 + pyte 终端模型。"""

from .console import require_primitive
from .host import LocalPtyHost

__all__ = ["LocalPtyHost", "require_primitive"]
