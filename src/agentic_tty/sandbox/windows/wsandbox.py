"""`winsandbox` 绑定：把 `vendor/winsandbox` 接进 `sys.path` 再惰性导入。

`winsandbox` 是仓库 `vendor/` 里的长期依赖（in-process nanobind 扩展，**不走 pip
安装**），由 `core.runtime.vendor.ensure_vendor_on_path()` 接进来——目录名即导入名，
所以这里 import 的是 `winsandbox`，而不是上游发行时的 `win_sandbox`。

惰性导入：别的模式不该被它拖住。

扩展本身没有类型信息，用到的那一小块接口由下面两个 `Protocol` 说明（签名抄自
`_native`）。
"""

from __future__ import annotations

from types import ModuleType
from typing import Protocol

from ...core.runtime.errors import DependencyMissing

_winsandbox: ModuleType | None = None


class SandboxProcess(Protocol):
    """一次受限 spawn 出来的子进程。"""

    @property
    def pid(self) -> int: ...

    def terminate(self, exit_code: int = 1) -> None:
        """强杀——连着作业里的整棵树。"""
        ...

    def query_process_list(self) -> list[int]:
        """作业当前的成员 pid（含根进程）。"""
        ...

    def poll_exit(self) -> tuple[int, str] | None:
        """非阻塞查退出结果 `(退出码, 原因)`；仍在运行返回 `None`。"""
        ...

    @property
    def stdin_handle(self) -> int:
        """stdin 写端的句柄（只有 `pipe_stdio=True` 起出来的进程才有，否则 0）。

        三个句柄的**所有权归调用方**：库不再关它们，用完自己关。
        """
        ...

    @property
    def stdout_handle(self) -> int:
        """stdout 读端的句柄（见 `stdin_handle` 的所有权说明）。"""
        ...

    @property
    def stderr_handle(self) -> int:
        """stderr 读端的句柄（见 `stdin_handle` 的所有权说明）。"""
        ...


class SandboxInstance(Protocol):
    """一次会话的沙箱实例：受限 spawn ＋ 收尾（作业与目录授权一起收）。"""

    def start_process(
        self,
        command_line: str,
        working_dir: str,
        workspace_write: bool = True,
        hpcon: int | None = None,
        env: dict[str, str] | None = None,
        pipe_stdio: bool = False,
    ) -> SandboxProcess: ...

    def shutdown(self) -> None: ...


def require_winsandbox() -> ModuleType:
    """惰性导入 winsandbox；不可用则抛 `DependencyMissing`。"""
    global _winsandbox
    if _winsandbox is None:
        from ...core.runtime.vendor import ensure_vendor_on_path

        ensure_vendor_on_path()
        try:
            import winsandbox
        except Exception as exc:
            raise DependencyMissing(f"winsandbox 不可用（应在仓库 vendor/ 下）: {exc}") from exc
        _winsandbox = winsandbox
    return _winsandbox
