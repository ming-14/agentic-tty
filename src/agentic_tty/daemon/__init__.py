"""守护进程：装配与生命周期，不含业务。

它把**请求处理层**与**传输层**拼成一个进程，并管单实例、启动顺序、停止收尾。

**机制只认一个注入的请求处理协议**（`handler.RequestHandler`）——它不知道 `sid`、
等待引擎、订阅这些东西的存在。所以 `service` 还没写出来时由 `example_daemon` 顶上，
将来换成正式实现也不用动这里的机制一行（架构设计 §12）。
"""

from __future__ import annotations
