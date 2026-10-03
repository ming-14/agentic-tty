"""请求处理层的接缝：守护进程对它的全部要求。

守护进程只做"把消费者投进来的请求交给它、把它交出的答复回调给消费者"，**不认识请求和
答复里装的是什么**——那是消费者（开服务器的那一层）与线协议之间的事。所以这里的类型
一律不透明：`object` 进、`object` 出，守护进程连拆开看一眼都不干。

这和核心层用 `HostFactory` 倒置宿主是同一手法：换请求处理层不用动 `daemon` 的机制一行。

**所有方法都只在所有者线程上被调用**，实现里不需要任何锁。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class Reply:
    """一条待交回消费者的答复。

    `request` 是它归属的**原始请求对象**（`handle` 收到的那个）。延迟答复必须带上它：
    消费者靠请求的身份把答复送回对的那条连接——`mid` 只在一条连接内才有意义，不能当键。

    `answer` 的内容守护进程一概不解释，原样交回。
    """

    request: object
    answer: object = None


@runtime_checkable
class RequestHandler(Protocol):
    """请求处理层。"""

    def handle(self, request: object) -> Reply | None:
        """处理一条请求。

        返回 `None` 表示**已登记等待**，答复稍后由 `poll` 交出——等待不能在这里阻塞，
        否则一个慢等待会把所有会话冻住。`poll` 交出的延迟答复必须带上本方法收到的那个
        请求对象（`Reply.request`）。
        """
        ...

    def poll(self) -> list[Reply]:
        """交出此刻已经可以回答的答复（可能为空）。"""
        ...

    def pending(self) -> int:
        """还有多少条请求压着等（draining 时靠它判断要不要等）。"""
        ...

    def failure(self, request: object, error: BaseException) -> Reply:
        """`handle` 抛了异常：把它变成一条明确的失败答复。

        守护进程不会替本层组答复（它不认识报文），但也不能让消费者干等，所以异常
        一律走这里。失败长什么样由本层决定。
        """
        ...

    def on_input(self, key: str, data: bytes) -> None:
        """接下一段上行字节；`key` 的语义由消费者定（如会话 uid）。"""
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
