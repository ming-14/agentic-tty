"""Windows 沙箱实现：`vendor/winsandbox` 的受限 spawn，pty 与 subprocess 两条 stdio 路径。"""

from .launch import make_launcher, make_process_launcher

__all__ = ["make_launcher", "make_process_launcher"]
