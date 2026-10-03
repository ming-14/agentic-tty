"""守护进程：**就是那个进程**——生命周期、承载核心层、转发、装配。

四件事：

1. **自身的生命周期** —— 单实例、依赖检查、数据目录、启动顺序（可回滚）、停止收尾。
2. **承载核心层** —— 做唯一的**所有者线程**：核心层的驱动全在它上面发生。
3. **接入点** —— 本机管道（`transport` 的 `pipe://`）上的唯一口子：把线协议解成
   `Envelope` 投进接缝、把答复按请求身份写回。
4. **装配** —— `assembly.py` 把 core 装进默认请求处理层（`kernel.py`）再交给 `Daemon`；
   `python -m agentic_tty.daemon` 直接起（实例名取自 `config`，`--cwd` 只决定从哪儿起）。

**不实现网络**：接入点是本机 IPC，不是 TCP。它用 `protocol` 装帧、用 `transport` 搬字节，
但**接缝上的报文对它不透明**——它只把请求转给请求处理层，把答复转回去。

**哪几份碰 core**：只有 `kernel.py` / `assembly.py` / `__main__.py`；其余（`server.py` /
`access_point.py` / `handler.py` / `platform/` / `config.py` / `errors.py`）是"机制"，
一行 core 都不碰——白名单由 AST 断言按文件强制。
"""
