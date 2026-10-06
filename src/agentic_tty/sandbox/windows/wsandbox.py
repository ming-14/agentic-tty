"""`winsandbox` 绑定：把 `vendor/winsandbox` 接进 `sys.path` 再惰性导入。

`winsandbox` 是仓库 `vendor/` 里的长期依赖（in-process nanobind 扩展，**不走 pip
安装**），由 `core.runtime.vendor.ensure_vendor_on_path()` 接进来——目录名即导入名，
所以这里 import 的是 `winsandbox`，而不是上游发行时的 `win_sandbox`。

惰性导入：别的模式不该被它拖住。
"""

from __future__ import annotations

from types import ModuleType

from ...core.runtime.errors import DependencyMissing

_winsandbox: ModuleType | None = None


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
