"""端口定义：核心层需要宿主提供什么。

方法按**允许调用的线程**划分（终端模型由单一线程独占），而不是按功能划分。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable

# 内置模式的标签。模式是开放字符串，由接入方定义，core 不校验。
PTY = "pty"
SUBPROCESS = "subprocess"
LOCALPTY = "localpty"


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


@dataclass(frozen=True, slots=True)
class HostMetadata:
    """终端元数据。

    `title` 取程序设的窗口标题（OSC 0/2）。`pywezterm` 的标题接口不工作，那个宿主
    给的是占位值（见 `PtyHost.metadata`）；`localpty` 能拿到真的。
    `cwd` 优先取 OSC 7（程序自己 `cd` 之后的真实目录），拿不到则退回会话创建时的目录。
    """

    title: str | None = None
    cwd: str | None = None


@runtime_checkable
class HostLifecycle(Protocol):
    """宿主共同生命周期。"""

    @property
    def pid(self) -> int | None:
        """子进程 PID；未启动或已关闭为 None。"""
        ...

    def read(self, max_bytes: int = 65536, timeout: float | None = 0.2) -> bytes:
        """从主输出流读一段。**只允许读线程调用。**

        空返回值是本轮无数据（超时），不代表 EOF；是否排空问 `poll_eof()`。
        """
        ...

    def write(self, data: bytes) -> None:
        """写入子进程输入。**唯一调用者必须是写线程**（缓冲写满时会阻塞）。"""
        ...

    def try_wait(self) -> int | None:
        """非阻塞查询退出码；None 表示仍在运行。

        宿主已释放后不再作答，一律返 `None`——退出码在会话停止时就已经取走。
        """
        ...

    def poll_eof(self, stream: Stream = Stream.STDOUT) -> bool:
        """本路输出是否已排空。**只允许读线程调用。**

        排空只有宿主自己判得准（PTY 拿不到真 EOF），驱动方只取结果，不推测。
        """
        ...

    def kill(self) -> None:
        """终止子进程。"""
        ...

    def descendants(self) -> tuple[int, ...]:
        """本会话进程树里**除根进程外**的成员 pid（升序）。

        **轮询式观测**。观测不到时显式报错，不静默返回空元组——空元组是"确实没有
        子进程"，两者混同会让"子进程启动→终止"这类条件静默失效。
        """
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

    def rebuild_bytes(self) -> bytes:
        """生成**重建字节**（RIS + 模式恢复 + scrollback + 可见区）。

        喂进一个空终端模型即可还原到当前状态。**不是屏幕内容**——要屏幕内容用
        `screen_text()` / `full_text()`。
        """
        ...

    def screen_text(self) -> str:
        """**可见屏幕**的纯文本（行用 `\\n` 连接，去尾部空白）。"""
        ...

    def full_text(self) -> str:
        """**全量输出**：含滚动历史的可见文本。

        与字节流全量（字节日志）是两回事——这里已经过终端模型解析。
        """
        ...

    def screen_cells(self) -> tuple[tuple[str, ...], ...]:
        """**可见屏幕**的字符格栅。宽字符占两格，续格为空串；行不铺满整宽。"""
        ...

    def render_svg(self) -> str:
        """把当前**可见屏幕**渲染成 SVG（按需生成）。"""
        ...

    def render_image(self, *, scale: float = 1.0, fmt: str = "png") -> bytes:
        """把当前**可见屏幕**渲染成位图。`fmt` ∈ `png` / `jpg` / `jpeg` / `bmp`。"""
        ...

    def metadata(self) -> HostMetadata:
        """当前元数据快照（标题 / cwd）。"""
        ...


@runtime_checkable
class ProcessHost(HostLifecycle, Protocol):
    """子进程宿主：双管道，无终端模型。"""

    def read_stderr(self, max_bytes: int = 65536, timeout: float | None = 0.2) -> bytes:
        """从 stderr 读一段。**只允许读线程调用。**"""
        ...

    def close_stdin(self) -> None:
        """关闭 stdin 发 EOF（`cat`、`python -` 这类程序在等它）。"""
        ...


HostFactory = Callable[[SessionSpec], HostLifecycle]
"""宿主工厂：装配层注入，核心层只依赖这个可调用对象。"""
