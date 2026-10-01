"""运行时层：宿主适配、进程树、平台隔离、单实例、依赖接入。

**唯一**可以 import 原生扩展与平台 API 的层。

导入本包时会把仓库里的 `vendor/` 接进 `sys.path`（见 `vendor.py`），
这样长期依赖不需要 pip 安装，也不需要调用者配 `PYTHONPATH`。
"""

from __future__ import annotations

from .vendor import ensure_vendor_on_path

ensure_vendor_on_path()
