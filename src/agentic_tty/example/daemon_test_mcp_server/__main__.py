"""MCP 验证台入口：起一个 stdio 的 MCP 服务端（由 MCP 客户端拉起）。

    python -m agentic_tty.example.daemon_test_mcp_server

守护进程不由用户手动起——本进程自己起一个，退出时一起收掉。

实例名：随机（`mcp-<8 位>`）
端点名：`config.endpoint_name(实例名)`
运行时目录：`config.runtime_dir(实例名)`
"""

from __future__ import annotations

import sys

from ...foundation.logs import configure, get_logger
from .server import build_server
from .supervisor import Supervisor

_logger = get_logger("example.daemon_test_mcp_server")


def main() -> int:
    configure()
    supervisor = Supervisor()
    _logger.info("MCP 服务端就绪 实例=%s", supervisor.instance)
    try:
        build_server(supervisor).run()  # stdio：阻塞到 stdin 关闭
    finally:
        supervisor.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
