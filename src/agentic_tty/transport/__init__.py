"""外网兼容层：让核心能力可以被进程外访问。

**这一层是客户端与守护进程共用的**：同一个包、同一份实现，`listen()` 给守护进程
用、`connect()` 给客户端用，只是角色不同（架构设计 §3 的两条链）。

分工：

- `stream.py`  抽象：一条双向字节流 / 一个监听器 / 地址
- `tcp.py`     TCP 实现（同机 loopback 与跨机是同一份代码）
- `registry.py` 按地址 scheme 分派——**加新传输方式只动这里**
- `channel.py` 把字节连接包成"收发帧"，帧格式本身在 `protocol`

传输**不认识帧**，`protocol` **不认识 socket**，中间那层胶水就是 `channel`。
"""

from __future__ import annotations
