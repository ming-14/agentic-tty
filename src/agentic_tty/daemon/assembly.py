"""装配层：把 core 装进请求处理层，交给 `Daemon`，起它。

与 `core` 自带 `core.runtime` 是同一手法——**默认实现自带，注入点保留**：
`build(handler_factory=…)` 可以换成任何请求处理层，`Daemon` 与 core 一行不用改。

**强制退出在这里**：`stop()` 带超时返回"是否停干净"，拿到否就 `os._exit`——库不杀进程。
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

from ..core.runtime.host_factory import check_dependencies
from ..foundation.logs import configure, get_logger
from ..foundation.paths import default_runtime_dir
from ..transport.pipe import pipe_address
from .config import DaemonConfig
from .handler import RequestHandler
from .kernel import KernelHandler
from .server import Daemon

_logger = get_logger("daemon.assembly")

DEFAULT_NAME = "default"
"""默认实例名。项目前缀由 `transport` 加在**端点**上（`pipe://agentic-tty-<名字>`），
所以这里只管实例名——别在名字里再写一遍 `agentic-tty-`。"""


def build(
    name: str = DEFAULT_NAME,
    *,
    runtime_dir: Path | None = None,
    handler_factory: Callable[[], RequestHandler] | None = None,
) -> Daemon:
    """装配一个守护进程：请求处理层（core 在里面）＋ 接入点（由名字派生）。

    不给 `handler_factory` 就用自带的 `KernelHandler`。

    运行时目录在这里**定一次**，同时喂给配置与请求处理层——否则状态里报的接入点地址会
    少掉目录那一段（`Daemon` 自己也会算一遍 `runtime_dir or default_runtime_dir(name)`）。
    """
    effective = runtime_dir or default_runtime_dir(name)
    config = DaemonConfig(name=name, runtime_dir=effective, listen=name)
    factory = handler_factory or (lambda: KernelHandler(listen=pipe_address(name, effective)))
    return Daemon(config, factory, check_dependencies=check_dependencies)


def run(name: str = DEFAULT_NAME, *, runtime_dir: Path | None = None) -> int:
    """装配 → 起 → 跑；收尾没在预算内完成就强制退出。"""
    configure()
    daemon = build(name, runtime_dir=runtime_dir)
    daemon.start()
    _logger.info("守护进程已就绪 pid=%s name=%s", os.getpid(), name)
    clean = True
    try:
        daemon.run()
    finally:
        clean = daemon.stop()
    if not clean:
        _logger.error("收尾超时，强制退出")
        os._exit(1)
    return 0
