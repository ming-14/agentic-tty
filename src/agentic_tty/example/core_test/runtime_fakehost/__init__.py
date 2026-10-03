"""假宿主：给 core 提供宿主能力的另一种实现。

与 `core/runtime/` 处于**同一个生态位**——两者接的是 core 定义的同一组端口
（`HostFactory` / `HostLifecycle` / `TerminalHost` / `ProcessHost`），
装配时由装配点注入、供 core 消费。除这一点外与 `core/runtime/` **毫无关系**：
不模仿它的内部结构，也不被它认识。

它不做 VT 解析（`ingest` 只把字节追加到一处纯文本缓冲），因此能验证的是
"摄入与日志相邻"、视图链路、生命周期这类核心语义，出不了真实的屏幕。
"""

from __future__ import annotations

from .fake_host import FakeHost, FakeProgram

__all__ = ["FakeHost", "FakeProgram"]
