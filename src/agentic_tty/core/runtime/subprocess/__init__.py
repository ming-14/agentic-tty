"""`subprocess` 宿主：`Popen` 双管道，无终端模型。"""

from .host import SubprocessHost
from .pipes import ProcessConsole, ProcessLauncher, ProcessPrimitive, read_fd

__all__ = [
    "ProcessConsole",
    "ProcessLauncher",
    "ProcessPrimitive",
    "SubprocessHost",
    "read_fd",
]
