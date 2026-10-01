"""端口定义（依赖倒置的接缝）。

核心层**定义**它需要宿主提供什么；运行时层提供实现。因此核心层不会出现
`import pywezterm`，可以脱离真实 PTY 完整单测。

## 线程归属是端口契约的一部分

终端模型是可变的共享状态，必须由**同一个线程**独占读写。因此端口方法按
**允许调用的线程**划分，而不是按功能划分（详见 core 设计文档的线程归属表）。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable

# 内置模式的标签。模式是**开放字符串**：标签由接入方（会话实现 / 宿主）自己定义，
# core 不做校验；这两个只是内置的终端形态所用的标签。
PTY = "pty"
SUBPROCESS = "subprocess"


class Stream(StrEnum):
    """会话输出流。Pty 只有 STDOUT，子进程有 STDOUT / STDERR。"""

    STDOUT = "stdout"
    STDERR = "stderr"


@dataclass(frozen=True, slots=True)
class SessionSpec:
    """创建一个会话宿主所需的全部输入。"""

    mode: str
    argv: Sequence[str]
    cols: int = 80
    rows: int = 24
    cwd: str | None = None
    env: Mapping[str, str] = field(default_factory=dict)
    encoding: str = "utf-8"


@dataclass(frozen=True, slots=True)
class HostMetadata:
    """由终端模型解析出的元数据（标题、当前目录等）。"""

    title: str | None = None
    cwd: str | None = None


@runtime_checkable
class HostLifecycle(Protocol):
    """宿主共同生命周期。每个方法都注明了唯一允许的调用线程。"""

    @property
    def pid(self) -> int | None:
        """子进程 PID；未启动或已关闭为 None。"""
        ...

    def read(self, max_bytes: int = 65536, timeout: float | None = 0.2) -> bytes:
        """从主输出流读取至多 `max_bytes` 字节。**只允许读线程调用，不碰终端模型。**

        空返回值表示本轮无数据（超时），不代表 EOF；EOF 由 `try_wait()` 判断。
        """
        ...

    def write(self, data: bytes) -> None:
        """把字节写入子进程输入。**唯一调用者必须是该会话的写线程。**

        写会在缓冲写满时阻塞；放在事件循环上会冻结整个进程。
        """
        ...

    def try_wait(self) -> int | None:
        """非阻塞查询退出码；None 表示仍在运行。"""
        ...

    def kill(self) -> None:
        """终止子进程。"""
        ...

    def close(self) -> None:
        """释放宿主资源（幂等）。"""
        ...


@runtime_checkable
class TerminalHost(HostLifecycle, Protocol):
    """终端宿主：PTY + 终端模型。"""

    def ingest(self, data: bytes) -> bytes:
        """把一段输出喂进终端模型，返回模型要回写给应用的应答字节。"""
        ...

    def resize(self, cols: int, rows: int) -> None:
        """把 PTY 与终端模型一起改成 `cols × rows`。"""
        ...

    def snapshot(self) -> bytes:
        """生成重建用字节（RIS + scrollback 重放 + 可见区 + 模式恢复）。"""
        ...

    def metadata(self) -> HostMetadata:
        """当前元数据快照。"""
        ...


@runtime_checkable
class ProcessHost(HostLifecycle, Protocol):
    """子进程宿主：双管道，无终端模型。"""

    def read_stderr(self, max_bytes: int = 65536, timeout: float | None = 0.2) -> bytes:
        """从 stderr 读取至多 `max_bytes` 字节。**只允许读线程调用。**"""
        ...

    def close_stdin(self) -> None:
        """关闭 stdin，向子进程发 EOF（`cat`、`python -` 这类程序在等它）。"""
        ...


HostFactory = Callable[[SessionSpec], HostLifecycle]
"""由装配层注入；核心层只依赖这个可调用对象，不依赖任何具体实现。"""
