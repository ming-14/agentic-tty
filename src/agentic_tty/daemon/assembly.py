"""装配层：把 core 装进请求处理层，交给 `Daemon`，起它。

`build(config, handler_factory=…)` 可以换成任何请求处理层，`Daemon` 与 core 一行不用改。

**模式表在这里定**：core 只认识内置那几种，沙箱是能力实现（core 反过来不认识它），
所以"这台守护进程提供哪些模式"由装配处合并后一次性交给注册表。

**强制退出在这里**：`stop()` 带超时返回"是否停干净"，拿到否就 `os._exit`——库不杀进程。
"""

from __future__ import annotations

import os
from collections.abc import Callable

from ..config import DaemonConfig
from ..core.session.registry import DEFAULT_KINDS, SessionRegistry
from ..foundation.logs import configure, get_logger
from ..sandbox import sandbox_kinds
from .handler import RequestHandler
from .kernel import KernelHandler
from .server import Daemon

_logger = get_logger("daemon.assembly")


def default_registry(config: DaemonConfig) -> SessionRegistry:
    """默认的`标签 → 会话形态`：core 的内置形态 ＋ 沙箱。

    注册表的 `kinds` 是**替换**不是合并，所以必须自己把 `DEFAULT_KINDS` 铺进来；
    沙箱只往上面**加**一个标签，core 加了模式这里不会漏。

    `config.sandbox_workspace_write` 在这里一次性定死——请求侧没有那个字段。
    """
    return SessionRegistry(
        kinds={
            **DEFAULT_KINDS,
            **sandbox_kinds(workspace_write=config.sandbox_workspace_write),
        }
    )


def build(
    config: DaemonConfig, *, handler_factory: Callable[[str], RequestHandler] | None = None
) -> Daemon:
    """按配置装配守护进程：请求处理层（core 在里面）＋ 接入点。

    不给 `handler_factory` 就用自带的 `KernelHandler`（配 `default_registry(config)`）；
    地址由 `Daemon` 算好后交给它。
    """
    factory = handler_factory or (
        lambda endpoint: KernelHandler(default_registry(config), endpoint=endpoint)
    )
    return Daemon(config, factory)


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
