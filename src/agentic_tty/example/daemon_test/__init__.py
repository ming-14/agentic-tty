"""守护进程的验证台：**纯消费者**——连一个已经在跑的守护进程，一切经接入点往返。"""

from __future__ import annotations

from ...config import endpoint_name, lock_name, runtime_dir
from ...transport.pipe import pipe_address


def address(instance: str) -> str:
    """接入点地址——与守护进程那边用同一套命名算出来。"""
    return pipe_address(endpoint_name(instance), runtime_dir(instance))


def lock(instance: str) -> str:
    """单实例锁名——配合 `foundation.instance.is_held()` 只读地问"它在不在"。"""
    return lock_name(instance, runtime_dir(instance))
