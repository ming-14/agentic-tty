"""直连 core 的 MCP 验证台：**本进程里装一个核心层**，把它的能力经 MCP（stdio）暴露出去。

    python -m agentic_tty.example.core_test_mcp_server

与 `daemon_test_mcp_server` 那台是**同一套工具面**，差别只在后端：这台没有守护进程、没有
接入点、没有协议——工具直接调 core（见 `core.py`）。MCP 客户端（host）拉起本进程即可用。
"""
