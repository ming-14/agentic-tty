"""外网兼容层：让核心能力可以被进程外访问。

**这一层是客户端与守护进程共用的**：同一个包、同一份实现，`listen()` 给守护进程
用、`connect()` 给客户端用，只是角色不同（架构设计 §3 的两条链）。

分工：

- `stream.py`  抽象：一条双向字节流 / 一个监听器 / 地址
- `tcp.py`     TCP 实现（同机 loopback 与跨机是同一份代码）
- `pipe.py`    本机管道实现（Windows 命名管道 / POSIX `AF_UNIX`）——**不是网络**，
               守护进程用它挂接入点，同机的消费者连进来；一个名字上可以有多条连接
- `registry.py` 按地址 scheme 分派——**加新传输方式只动这里**

**这一层只搬字节，不认识帧。** 帧的编解码全在 `protocol`，把两者接起来的是
`protocol.frame.FrameReader`——它只要求装配方注入一个"返回 bytes 的函数"，所以
传输不需要为帧做任何事，`protocol` 也不需要认识连接。
"""

from __future__ import annotations
