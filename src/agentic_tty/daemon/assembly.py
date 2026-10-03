"""装配层：把 core 装进请求处理层，交给 `Daemon`，起它。

与 `core` 自带 `core.runtime` 是同一手法——**默认实现自带，注入点保留**：
`build(config, handler_factory=…)` 可以换成任何请求处理层，`Daemon` 与 core 一行不用改。

**强制退出在这里**：`stop()` 带超时返回"是否停干净"，拿到否就 `os._exit`——库不杀进程。
"""

from __future__ import annotations

import os
from collections.abc import Callable

from ..config.names import endpoint_name, runtime_dir
from ..core.runtime.host_factory import check_dependencies
from ..foundation.logs import configure, get_logger
from ..transport.pipe import pipe_address
from .config import DaemonConfig
from .handler import RequestHandler
from .kernel import KernelHandler
from .server import Daemon

_logger = get_logger("daemon.assembly")


def endpoint_address(config: DaemonConfig) -> str:
    """配置指向的那个接入点地址——与 `Daemon` 实际挂的是同一个（含运行时目录那一段）。"""
    if config.listen is None:
        return ""
    directory = config.runtime_dir or runtime_dir(config.name)
    return pipe_address(endpoint_name(config.listen), directory)


def build(
    config: DaemonConfig, *, handler_factory: Callable[[], RequestHandler] | None = None
) -> Daemon:
    """按配置装配守护进程：请求处理层（core 在里面）＋ 接入点。

    不给 `handler_factory` 就用自带的 `KernelHandler`。
    """
    address = endpoint_address(config)
    factory = handler_factory or (lambda: KernelHandler(listen=address))
    return Daemon(config, factory, check_dependencies=check_dependencies)


def run(config: DaemonConfig) -> int:
    """装配 → 起 → 跑；收尾没在预算内完成就强制退出。"""
    configure()
    daemon = build(config)
    daemon.start()
    _logger.info("守护进程已起来 pid=%s name=%s", os.getpid(), config.name)
    clean = True
    try:
        daemon.run()
    finally:
        clean = daemon.stop()
    if not clean:
        _logger.error("收尾超时，强制退出")
        os._exit(1)
    return 0
