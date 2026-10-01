"""把不安装的长期依赖接进 `sys.path`。

`vendor/` 是仓库里的长期依赖目录（原生扩展等），**不走 pip 安装**。
从本文件位置向上找 `vendor/`，因此用哪个解释器、从哪个目录起都一样，
不需要调用者配 `PYTHONPATH`。

必须在任何 `import pywezterm` **之前**完成——本模块自身不导入任何扩展，
真正的导入发生在宿主模块里。
"""

from __future__ import annotations

import sys
from pathlib import Path


def find_vendor_dir() -> Path | None:
    """从本文件位置向上找 `vendor/`。"""
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "vendor"
        if candidate.is_dir():
            return candidate
    return None


def ensure_vendor_on_path() -> Path | None:
    """把 `vendor/` 接进 `sys.path`（幂等）；找不到返回 None。"""
    vendor = find_vendor_dir()
    if vendor is None:
        return None
    text = str(vendor)
    if text not in sys.path:
        sys.path.insert(0, text)
    return vendor
