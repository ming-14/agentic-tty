"""请求处理层的接缝：守护进程对它的全部要求。

`daemon/` 的机制只认这个协议，不知道 `sid`、等待引擎、订阅的存在。谁来实现它都行
——生产环境是 `service`，演示里是 `example_daemon`。这和核心层用 `HostFactory` 倒置
宿主是同一手法。

**所有方法都只在所有者线程上被调用**，所以实现里不需要任何锁。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ..protocol.envelope import Envelope


@dataclass(frozen=True, slots=True)
class Reply:
    """一条待发出的响应：控制帧，外加可选的字节帧负载。

    `binary` 不为空时，字节帧的键是 `envelope.mid`——客户端据此把这段字节挂回它
    发起的那条请求。
    """

    envelope: Envelope
    stream: str = "stdout"
    binary: bytes | None = None


@runtime_checkable
class RequestHandler(Protocol):
    """请求处理层。"""

    def handle(self, envelope: Envelope) -> Reply | None:
        """处理一条请求。

        返回 `None` 表示**已登记等待**，响应稍后由 `poll` 交出——等待不能在这里阻塞，
        否则一个慢等待会把所有会话冻住。
        """
        ...

    def poll(self) -> list[Reply]:
        """交出此刻已经可以回答的响应（可能为空）。"""
        ...

    def on_input(self, key: str, data: bytes) -> None:
        """接下一段上行字节；`key` 是会话 `sid`。"""
        ...

    def pump(self) -> None:
        """按轮推进（例如排空各会话的读桥、推进等待判定）。"""
        ...

    def shutdown(self) -> None:
        """收尾：关掉手上的会话。

        可能长时间阻塞（宿主关闭在部分平台上要等数百秒），调用方会把它放在线程里
        并带超时。
        """
        ...
