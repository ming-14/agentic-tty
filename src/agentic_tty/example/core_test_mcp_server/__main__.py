"""MCP 验证台入口：起一个 stdio 的 MCP 服务端（由 MCP 客户端拉起）。

    python -m agentic_tty.example.core_test_mcp_server

核心层装在本进程里，没有守护进程、没有连接——会话随本进程一起收掉。
"""

from __future__ import annotations

import sys

from ...foundation.logs import configure, get_logger
from .core import Core
from .server import build_server

_logger = get_logger("example.core_test_mcp_server")


def main() -> int:
    configure()
    _logger.info("MCP 服务端就绪（直连 core）")
    build_server(Core()).run()  # stdio：阻塞到 stdin 关闭，收尾由 lifespan 做
    return 0


if __name__ == "__main__":
    sys.exit(main())
