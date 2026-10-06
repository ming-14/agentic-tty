"""沙箱会话：子进程关在受限令牌里起，可写区只有工作区与它自己的私有 temp。

与核心层的关系是"能力实现"：它只依赖 `core`（端口、会话类、终端模型与渲染），核心层
反过来不认识它——模式由装配处把 `sandbox_kinds()` 合并进 `SessionRegistry` 才生效。
"""

from .kinds import SANDBOX_PTY, pty_host_factory, sandbox_kinds

__all__ = ["SANDBOX_PTY", "pty_host_factory", "sandbox_kinds"]
