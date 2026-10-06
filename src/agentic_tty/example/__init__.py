"""示例层：连到某一层、专门测那一层能力的消费者。

- `core_test_console/` —— **进程内**的台：直连核心层，不套守护进程
- `daemon_test_console/` —— **跨进程**的台，**纯消费者**：只 import 公共层，连守护进程
- `ui/` —— 验证台共用的 Tk 控件，**纯界面，不认识任何一层**

两台差别只在**挂载点**。**只依赖别人，不被任何一层依赖。**

两台都从 `src/` 起（**不依赖安装**；或给 `PYTHONPATH=src`）：

    cd src && python -m agentic_tty.example.core_test_console
    cd src && python -m agentic_tty.example.daemon_test_console
"""
