"""守护进程的 MCP 验证台：**自己起一个守护进程**，把它的能力经 MCP（stdio）暴露出去。

    python -m agentic_tty.example.daemon_test_mcp_server

MCP 客户端（host）拉起本进程，本进程再起一个守护进程——实例名随机，管道名与锁名随之
随机，所以同时开几份互不相撞。host 那边配好命令就能用，不必先手动起守护进程。

名字不在这里发明：实例名随机，但"一个实例在操作系统里叫什么"一律走 `config.constants`
那套派生，两端各算一次必然一致。
"""

from __future__ import annotations

from ...config import endpoint_name, runtime_dir
from ...transport.pipe import pipe_address


def address(instance: str) -> str:
    """接入点地址——与守护进程那边用同一套命名算出来。"""
    return pipe_address(endpoint_name(instance), runtime_dir(instance))
