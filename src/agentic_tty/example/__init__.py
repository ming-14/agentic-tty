"""示例层：连到某一层、专门测那一层能力的消费者。

一格一个层，格与格之间互不相干：`core_test/`（连核心层，Tk 测试台）、
`service.py` + `daemon.py`（服务端那一格）、`client.py`（客户端那一格）。
**只依赖别人，不被任何一层依赖。**

`python -m agentic_tty.example.core_test` 打开核心层的测试台。
"""
