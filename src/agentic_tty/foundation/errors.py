"""错误基类。

本工程所有错误都从这里派生，便于上层按类型统一兜底。
"""


class AgenticTtyError(Exception):
    """本工程所有错误的基类。"""
