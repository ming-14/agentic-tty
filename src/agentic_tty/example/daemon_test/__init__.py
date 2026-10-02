"""守护进程那一格的验证台：连守护进程，由它承载核心层。

与 `core_test/` 并列，但**挂载点不同**：

    core_test/    直连核心层（自己装 core + runtime），不套守护进程
    daemon_test/  把核心层交给守护进程承载，经它的接缝驱动

它与客户端命令处理层 / web 层同生态位（都是"直接消费核心层能力"的一方），但**不起
网络**：不开监听、不写端点文件、不碰 `protocol` / `transport`。

`python -m agentic_tty.example.daemon_test` 打开验证台。
"""
