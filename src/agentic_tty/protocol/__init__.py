"""线协议：通用底座 ＋ 按边界分区的消息 schema。

本层是两端之间**唯一**的契约，只依赖 `foundation`。两端可以跨机，因此这里的每个字段
都是"发出去就改不了"的格式：改动只能靠递增 `proto` 版本。

- `frame` / `envelope` / `response` / `errors`：**通用底座**，无领域语义，谁都 import。
- `contracts/`：**按边界分区**——`daemon_ipc`（守护进程 ↔ 下游消费者，载 `uid`）、
  `consumer_ipc`（下游消费者 ↔ 它的客户端，载 `sid`）。两份分区**零依赖、互不共享类型**。

帧从哪来由装配方注入（`FrameReader` 只要求一个"返回 bytes 的函数"），所以这一层
始终不认识连接、超时、socket。
"""
