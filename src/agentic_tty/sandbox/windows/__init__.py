"""Windows 沙箱实现：`vendor/winsandbox` 的受限 spawn + condrv 的伪终端。"""

from .launch import make_launcher

__all__ = ["make_launcher"]
