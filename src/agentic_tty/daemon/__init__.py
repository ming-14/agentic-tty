"""守护进程：**就是那个进程**——生命周期、承载核心层、转发、装配。

**不实现网络**：接入点是本机 IPC，不是 TCP。它用 `protocol` 装帧、用 `transport` 搬字节，
但**接缝上的报文对它不透明**——它只把请求转给请求处理层，把答复转回去。

`assembly.py` 把 core 装进默认请求处理层（`kernel.py`）再交给 `Daemon`；
`python -m agentic_tty.daemon` 直接起（实例名取自 `config`，`--cwd` 只决定从哪儿起）。
"""
