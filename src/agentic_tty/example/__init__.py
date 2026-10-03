"""示例层：连到某一层、专门测那一层能力的消费者。

- `core_test/` —— 直连核心层，不套守护进程
- `daemon_test/` —— 连守护进程（两个进程，走本机管道）
- `ui/` —— 验证台共用的 Tk 控件，**纯界面，不认识任何一层**

两台台子差别只在挂载点。**只依赖别人，不被任何一层依赖。**

两台都从 `src/` 起（**不依赖安装**；或给 `PYTHONPATH=src`）：

    cd src && python -m agentic_tty.example.core_test
    cd src && python -m agentic_tty.example.daemon_test
"""
