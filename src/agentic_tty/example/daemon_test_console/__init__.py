"""守护进程的验证台：**纯消费者**——连一个已经在跑的守护进程，一切经接入点往返。"""

from __future__ import annotations

from ...config import lock_name, resolve_endpoint, runtime_dir
from ...transport.pipe import pipe_address


def address(instance: str, endpoint: str | None = None) -> str:
    """接入点地址——与守护进程那边用同一套命名算出来（`endpoint` 显式给完整管道名时优先）。"""
    return pipe_address(resolve_endpoint(instance, endpoint), runtime_dir(instance))


def lock(instance: str) -> str:
    """单实例锁名——配合 `foundation.instance.is_held()` 只读地问"它在不在"。"""
    return lock_name(instance, runtime_dir(instance))
